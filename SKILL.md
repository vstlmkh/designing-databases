---
name: designing-databases
description: >
  Design, audit and tune relational databases on PostgreSQL and MySQL/InnoDB —
  table and key design, index strategy, slow-query and EXPLAIN-plan diagnosis,
  transaction isolation and locking, and zero-downtime migrations in any
  framework (Alembic, Django, Laravel, Doctrine, Prisma, TypeORM, Knex, Drizzle,
  Flyway, Liquibase, Atlas, golang-migrate). Use when the user wants to model
  tables, pick primary keys or column types, decide which index to add or drop,
  find out why a query or endpoint became slow, resolve deadlocks, lock waits,
  bloat or replication lag, or write and review a schema change — including when
  they never name the database and only say "this endpoint got slow", "add a
  field to this table", "the deploy locked everything" or "which index do we
  need". Not for NoSQL stores, and it never applies DDL without explicit
  approval.
license: MIT
metadata:
  version: "1.0.0"
---

# designing-databases

Relational engines fail in physical, deterministic ways: a page split, a lock
queue, a stale visibility map, a cardinality misestimate. Generic SQL advice
("add an index", "normalize it") misses all of them, and PostgreSQL and MySQL
diverge exactly where it matters. This skill works from evidence — the real
schema, the real plan, the real statistics — and names the engine mechanism
behind every recommendation.

## Contents

- Step 0 — classify before advising
- Routing
- Workflow A — design or review a schema
- Workflow B — diagnose and optimize
- Workflow C — ship a schema change
- Scripts
- Live database access
- Gotchas
- References

## Step 0 — classify before advising (non-negotiable)

Never answer a database question without these four facts. Two of them you can
usually read from the repo; ask only for what is genuinely missing.

1. **Engine and version** — `postgres` or `mysql` (InnoDB), major version.
   Look in docker-compose, CI config, the ORM driver, `.env`. If it is still
   unknown, ask; if the user cannot answer, answer for both and label each
   branch. Their storage engines make opposite choices about clustering, MVCC
   and locking, so a single answer is usually wrong for one of them.
2. **Workload** — OLTP (many small concurrent transactions) or analytics
   (few large scans); read/write ratio; table row counts and growth rate.
   An index that is free at 10k rows is a write tax at 500M.
3. **Access** — can you reach a database (live, replica, staging, or a local
   container), or is this an offline review of code and migrations?
4. **Blast radius** — is the target production? Then no DDL, no DML, no
   `EXPLAIN ANALYZE` on a writing statement without explicit approval.

## Routing

| The user wants | Do this | Load |
|---|---|---|
| A new table / schema / data model | Workflow A | `references/schema-design.md` |
| "Why is this slow?", a plan to read, an endpoint that degraded | Workflow B | `references/query-tuning.md` |
| Which index to add, drop, or reorder; index bloat | Workflow B, then index design | `references/indexing.md` |
| A migration written, reviewed, or made zero-downtime | Workflow C | `references/migrations.md` |
| Deadlocks, lock waits, lost updates, bloat, replication lag | Workflow B with the concurrency lens | `references/engines.md` |
| A health check of a whole existing database | Workflows A+B over the audit output | `references/live-access.md` |

## Workflow A — design or review a schema

- [ ] Write the invariants in prose first: what must be unique, what must exist,
      what must stay consistent with what. Those become constraints, not comments.
- [ ] Model to 3NF. Denormalize only against a named read path, and say in the
      DDL comment which query pays for it and what keeps the copy in sync.
- [ ] Choose keys: `BIGINT` identity by default; UUIDv7/ULID in a native binary
      type when IDs must be externally non-enumerable. Never random UUIDv4.
- [ ] Type every column deliberately — money as `NUMERIC`/`DECIMAL`, time as
      `TIMESTAMPTZ`, and `NOT NULL` unless the unknown value is a real business state.
- [ ] Index the child side of every foreign key. PostgreSQL never creates it;
      InnoDB auto-creates only a single-column one, which the real query usually
      needs widened.
- [ ] Write the 5–10 queries the table exists to serve, then design indexes for
      them with the ESR rule (Equality, Sort, Range). No query, no index.
- [ ] Run `schema_audit.py` on the DDL and resolve every ERROR before shipping.

## Workflow B — diagnose and optimize

Measure, then change one thing, then measure again. Never reorder these.

- [ ] **Find the real cost centre.** `pg_stat_statements` / MySQL slow log,
      ranked by *total* time (`calls × mean_time`), not worst single call.
- [ ] **Get an actual plan**, never a guess:
      `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) …` (PostgreSQL) or
      `EXPLAIN ANALYZE` / `EXPLAIN FORMAT=JSON` (MySQL 8.0.18+).
- [ ] **Read it with the script**, which does the loop multiplication and
      estimate-vs-actual math by hand-proof arithmetic: `explain_audit.py`.
- [ ] **Name the root cause** before proposing anything — stale statistics,
      non-sargable predicate, missing/misordered index, disk spill, or a plan
      shape driven by a misestimate. The fix follows from the cause.
- [ ] **Apply exactly one change**, re-run the plan, and compare buffers and
      time. Report both numbers.
- [ ] **Account for the write cost** of any index you add, and check
      `pg_stat_user_indexes` / `sys.schema_unused_indexes` for what it replaces.

## Workflow C — ship a schema change

- [ ] Classify the change: additive (safe), filling (needs backfill), or
      destructive (needs expand/contract across releases).
- [ ] Apply expand/contract for anything that renames, retypes, or removes:
      add new → dual-write → backfill in batches → switch readers → drop old,
      each in a separate deploy. Old application instances are still running
      while the migration lands.
- [ ] Guard every migration with `SET lock_timeout = '3s';` (PostgreSQL) or
      `SET SESSION lock_wait_timeout = 3;` (MySQL), and retry on timeout.
- [ ] Build indexes without blocking: `CREATE INDEX CONCURRENTLY` (PostgreSQL,
      outside any transaction) or `ALGORITHM=INPLACE, LOCK=NONE` (MySQL 8);
      reach for `gh-ost`/`pt-online-schema-change` when InnoDB forces a COPY.
- [ ] Backfill in bounded batches with its own commit per chunk — never one
      statement over the whole table.
- [ ] Run `migration_lint.py` on the migration directory and resolve every
      ERROR. Then state the expected lock level and duration for each statement.
- [ ] Write the rollback path. "Re-run the down migration" is not a plan if the
      up step dropped data.

## Scripts

Standard library Python 3, no install, no network. Run them — do not re-implement
their checks by reading SQL by eye.

```bash
# 1. Structural audit of DDL (pg_dump --schema-only, mysqldump --no-data, or migrations)
python3 scripts/schema_audit.py schema.sql --engine postgres [--format json]

# 2. Rank the real bottlenecks in an execution plan (loop math, misestimates, spills)
psql -XAt -c "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT …" > plan.json
python3 scripts/explain_audit.py plan.json [--format json]

# 3. Lint migrations for locking, rewriting and deploy-breaking operations
python3 scripts/migration_lint.py db/migrations --engine postgres [--min-severity warn]
```

All three exit `0` when clean, `1` when they found an ERROR-level problem, `2` on
bad input — so they work as a CI gate. Findings print to stdout, diagnostics to
stderr. Treat their output as a checklist to resolve, not as the final report:
every finding still needs your judgement about this schema's workload.

## Live database access

Read-only by default, always. Load `references/live-access.md` for the connection
recipes and the catalog query packs (`assets/pg_snapshot.sql`,
`assets/mysql_snapshot.sql`).

- Open the session read-only before anything else:
  `SET default_transaction_read_only = on;` (PostgreSQL) /
  `SET SESSION TRANSACTION READ ONLY;` (MySQL), plus
  `SET statement_timeout = '30s';`.
- `EXPLAIN ANALYZE` **executes** the statement. On an `INSERT`/`UPDATE`/`DELETE`,
  wrap it in `BEGIN; … ROLLBACK;` or you will modify production data.
- DDL, DML, `VACUUM FULL`, `OPTIMIZE TABLE`, index creation, `kill`/`pg_terminate_backend`:
  show the exact statement and its lock level, get an explicit yes, then run it.
- Never echo credentials. Read them from the environment, `~/.pgpass`,
  `~/.my.cnf`, or the project's own secret tooling.
- Prefer a replica or a staging copy with production-like volume. Say so when
  the numbers you report came from a replica.

## Gotchas

These defy reasonable assumptions and cost the most time when missed.

- **`Index Only Scan` in a plan does not mean the heap was skipped.** PostgreSQL
  checks the visibility map; if autovacuum is behind, `Heap Fetches` is large and
  the scan is quietly a normal index scan. Fix the vacuum, not the index.
- **`EXPLAIN` without `ANALYZE` is a guess.** It prints the planner's estimates,
  which is exactly the thing that is usually wrong. Never diagnose from it.
- **MySQL silently destroys an index seek on a type mismatch.** `WHERE
  phone = 5551234` on a `VARCHAR` column casts *every row* to a number — full
  scan, no warning. PostgreSQL raises an error instead. Same bug, opposite symptom.
- **Foreign-key indexing is asymmetric.** PostgreSQL never indexes the child
  column — an unindexed FK turns every parent delete into a full child scan under
  lock. InnoDB *requires* the index and silently creates a single-column one
  named after the constraint, so the MySQL risk is the opposite: an index you did
  not design, too narrow for the query that actually runs.
- **InnoDB secondary indexes store the primary key**, so PK width multiplies
  across every index on the table. A `CHAR(36)` UUID PK with six secondary
  indexes stores that 36-byte string seven times.
- **A partial index only matches when the planner can *prove* the query predicate
  implies the index predicate.** With `WHERE status IN ('QUEUED','PROCESSING')`
  on the index, a query filtering `status != 'COMPLETED'` falls back to a
  sequential scan, even though a human reads them as equivalent.
- **One `idle in transaction` connection blocks vacuum database-wide.**
  PostgreSQL cannot remove any tuple newer than the oldest open snapshot; tables
  and all their indexes swell. MySQL's analogue is a growing undo history list
  (`SHOW ENGINE INNODB STATUS` → "History list length").
- **`REPEATABLE READ` means different things in the two engines.** InnoDB takes
  gap and next-key locks, so a concurrent insert into a scanned range *waits*.
  PostgreSQL takes a snapshot and instead raises `could not serialize access`
  at write time. Code written against one deadlocks or errors on the other.
- **`SERIALIZABLE` without an application retry loop is an outage.** Serialization
  failures are expected behaviour, not a bug; the caller must retry with backoff.
- **`CREATE INDEX CONCURRENTLY` cannot run inside a transaction**, and when it
  fails it leaves an invalid index that still slows every write. Check
  `pg_index.indisvalid`, then `DROP INDEX CONCURRENTLY` and retry.
- **Adding a column with a constant `DEFAULT` is metadata-only** (PostgreSQL 11+,
  MySQL 8 `ALGORITHM=INSTANT`). With a *volatile* default — `now()`,
  `gen_random_uuid()` — it rewrites every row. The difference is invisible in
  the DDL text.
- **A blocked `ALTER TABLE` blocks everyone behind it.** It waits for an
  `ACCESS EXCLUSIVE` lock (PostgreSQL) or a metadata lock (MySQL) behind one long
  query, and every subsequent query queues behind *it*. Always `lock_timeout`.
- **PostgreSQL's default `random_page_cost = 4.0` assumes spinning disks.** On
  SSD/NVMe it makes the planner reject correct index plans; `1.1`–`1.5` matches
  reality. Check this before concluding "the planner ignores my index".
- **`work_mem` is per operation, not per connection.** A query with four joins
  and two sorts can allocate it six times over, simultaneously, per connection.
- **An index with zero scans on the primary may be critical on a replica.**
  Check replica telemetry before any `DROP INDEX`.
- **ORMs lie about cost, not correctness.** `SELECT DISTINCT` masking join
  fan-out, `COUNT(*)` for pagination, and lazy relations in a loop each look
  like one line of application code. Read the generated SQL, not the ORM call.

## References

- `references/schema-design.md` — normalization, key and type selection,
  constraints, JSON vs columns, multi-tenancy, soft deletes. Load in Workflow A.
- `references/indexing.md` — B+tree mechanics, ESR ordering, covering/partial/
  expression indexes, hash/GIN/BRIN, redundancy and bloat, the maintenance
  runbook. Load whenever an index is being added, dropped, or blamed.
- `references/query-tuning.md` — plan node catalogue, join algorithms, sargability,
  query anti-patterns and rewrites, statistics and memory settings. Load in
  Workflow B.
- `references/engines.md` — PostgreSQL heap/MVCC vs InnoDB clustered index and
  undo, write paths, isolation levels, locking, the five concurrency anomalies
  and their fixes. Load for any transaction, locking, deadlock or bloat question.
- `references/migrations.md` — expand/contract, lock-safe DDL per engine, and the
  per-framework commands and traps for Alembic, Django, Laravel, Doctrine,
  Prisma, TypeORM, Knex, Drizzle, Flyway, Liquibase, Atlas and golang-migrate.
  Load in Workflow C.
- `references/live-access.md` — read-only connection recipes, what to collect
  before advising, and how to read the snapshot output. Load when a database is
  reachable.
- `assets/report-template.md` — the output format for a schema or performance
  review. Load when the deliverable is a written report.
