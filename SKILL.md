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
  version: "1.2.0"
---

# designing-databases

Act as a database engineer who has operated PostgreSQL and MySQL in production:
reason from the engine's physical behaviour rather than from SQL folklore, and
say plainly what you could not establish.

Relational engines fail in physical, deterministic ways: a page split, a lock
queue, a stale visibility map, a cardinality misestimate. Generic SQL advice
("add an index", "normalize it") misses all of them, and PostgreSQL and MySQL
diverge exactly where it matters. So the workflows below start by establishing
facts: a recommendation grounded in the wrong engine or the wrong row count is
worse than no recommendation, because it will be deployed.

## Contents

- Operating rules
- Step 0 — classify before advising
- Routing
- Workflow A — design or review a schema
- Workflow B — diagnose and optimize
- Workflow C — ship a schema change
- What a complete answer contains
- Examples
- Scripts
- Live database access
- Long audits and multi-session work
- Gotchas
- References

## Operating rules

<operating_rules>
These hold in every workflow, in every answer.

1. **Investigate before answering.** Never speculate about a schema, query or
   plan you have not opened. When the repository holds the migrations, the ORM
   models, the compose file or `.env`, read them before answering.
2. **Quote the evidence, before you reason about it.** On anything large — a
   full schema dump, a long plan, a slow log — first extract the exact lines
   that bear on the question, verbatim and numbered, then reason only from that
   extract. If the artifact contains nothing relevant, write "No relevant lines
   found in <artifact>" and say what would be needed instead of reasoning from
   memory. A claim with nothing quoted behind it is a guess and is labelled as
   one.
3. **Name the engine.** Every statement about behaviour says whether it holds
   for PostgreSQL, for InnoDB, or for both. When the engine is still unknown,
   answer both branches and label them rather than silently assuming PostgreSQL.
4. **One change at a time**, each with the measurement that confirms it. When
   eight tweaks ship together, no one can attribute the numbers that moved.
5. **Stay inside the ask.** No speculative indexes, no refactors of untouched
   tables, no "while we're here" normalization. State what you deliberately
   left alone instead of fixing it.
6. **Weigh reversibility.** Reading catalogs, plans, counters and code is free —
   do it freely. Anything that writes, locks, or touches production — DDL, DML,
   `EXPLAIN ANALYZE` on a writing statement, `VACUUM FULL`, `OPTIMIZE TABLE`,
   `pg_terminate_backend`, `KILL` — is presented as the exact statement with its
   lock level and expected duration, and waits for an explicit yes.
7. **Report what stayed unknown, and say "I don't know".** Row counts you could
   not measure, replicas you could not see, statistics you could not read: list
   them as open questions rather than assuming the values that make the
   recommendation work. "I don't have enough information to assess this — give
   me X and Y and I will" is a complete, acceptable answer; a confident
   recommendation resting on an invented row count is not.
8. **Answer from the artifact, not from recollection.** When DDL, a plan, a
   config or source is in front of you, it overrides anything you remember about
   "how this usually looks". Engine behaviour that turns on a minor version
   (`ALGORITHM=INSTANT`, `ADD COLUMN` rewrites, index-skip scans) is stated as
   the condition — "on 8.0.29+ …, before that …" — unless you have actually read
   the version. Never invent a version number, a counter value, a config default
   you did not read, or a plan node that was not in the output.
</operating_rules>

## Step 0 — classify before advising

Four facts come before any advice. Read the repo for them in parallel — compose
files, migrations, ORM config and `.env` in one batch, not one file per turn —
and ask only for what the repo genuinely cannot answer.

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
4. **Blast radius** — is the target production? Then rule 6 applies to every
   statement you propose.

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
      type when IDs must be externally non-enumerable. Never random UUIDv4 — its
      insert position is random, which fragments the index and, on InnoDB,
      the table itself.
- [ ] Type every column deliberately — money as `NUMERIC`/`DECIMAL`, time as
      `TIMESTAMPTZ`, and `NOT NULL` unless the unknown value is a real business state.
- [ ] Index the child side of every foreign key. PostgreSQL never creates it;
      InnoDB auto-creates only a single-column one, which the real query usually
      needs widened.
- [ ] Write the 5–10 queries the table exists to serve, then design indexes for
      them with the ESR rule (Equality, Sort, Range). No query, no index.
- [ ] Run `schema_audit.py` on the DDL and resolve every ERROR before shipping.

## Workflow B — diagnose and optimize

Measure, then change one thing, then measure again. Keep this order: each step
exists to stop the next one from being guesswork.

- [ ] **Find the real cost centre.** `pg_stat_statements` / MySQL slow log,
      ranked by *total* time (`calls × mean_time`), not worst single call.
- [ ] **Get an actual plan**, never a guess:
      `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) …` (PostgreSQL) or
      `EXPLAIN ANALYZE` / `EXPLAIN FORMAT=JSON` (MySQL 8.0.18+).
- [ ] **Read it with the script**, which does the loop multiplication and
      estimate-vs-actual math by hand-proof arithmetic: `explain_audit.py`.
- [ ] **Name the root cause** before proposing anything — stale statistics,
      non-sargable predicate, missing/misordered index, disk spill, or a plan
      shape driven by a misestimate — and quote the node it comes from. The fix
      follows from the cause.
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

## What a complete answer contains

<success_criteria>
A complete answer carries five things, in this order:

1. the Step 0 facts you established, with the ones that stayed unknown marked;
2. the root cause named as an engine mechanism, with its evidence quoted;
3. one change, as runnable SQL/DDL for that engine and version;
4. its cost — lock level, expected duration, disk, write amplification;
5. the measurement that will confirm or refute it, and the rollback.

An answer missing 2, 4 or 5 is incomplete however correct the fix is: without
the mechanism it cannot be generalized, and without the cost and the measurement
it cannot be shipped safely.
</success_criteria>

### Audit the draft before sending it

Re-read your own answer and check every factual claim — a row count, a plan
node, a lock level, a version threshold, a config default — against the evidence
you actually have:

- backed by a quote from the artifact → keep it, with the quote visible;
- backed by an engine mechanism you can name but not quote here → keep it, and
  name the mechanism rather than presenting it as a measurement;
- backed by neither → delete it, or restate it as a hypothesis with the one
  check that would settle it ("if `Heap Fetches` is high, then …; run X").

Numbers are the usual casualty: no estimated duration, size, or row count
survives this pass unless it was measured or is shown as the arithmetic that
produced it.

## Examples

<examples>

<example>
<scenario>"Our /orders endpoint takes 5s since the table grew." Repo available, engine not named.</scenario>
<trajectory>
1. Read docker-compose, the ORM config and the migrations in one batch →
   PostgreSQL 16.3, `orders` ≈52M rows, OLTP.
2. Rank `pg_stat_statements` by `calls × mean_time` to find the real cost centre.
3. `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on the winner, through `explain_audit.py`.
4. Quote the node that owns the time, name the mechanism, propose one change.
</trajectory>
<response_shape>
PostgreSQL 16.3 · OLTP · plan taken on replica `db-r2` · growth rate unknown.

Root cause: the predicate is not sargable, so no index on `created_at` can be
used — `date_trunc()` is applied to the column, not to the bound.

    ->  Seq Scan on orders  (actual rows=182440 loops=1)
          Filter: (date_trunc('day', created_at) >= '2026-01-01')
          Rows Removed by Filter: 4817560

One change: rewrite as a half-open range so the existing index applies —
`WHERE created_at >= '2026-01-01' AND created_at < '2026-01-02'`. No new index
yet. Cost: none, it is a query edit. Verify by re-running the plan and comparing
`shared read` blocks and total time; roll back by reverting the predicate.
</response_shape>
</example>

<example>
<scenario>"Add a `status` field to `users`." Production MySQL, 40M rows.</scenario>
<trajectory>
Classify the change (additive), pin the exact MySQL 8 minor version because
`ALGORITHM=INSTANT` support moved across it, write the statement with its lock
profile and rollback — and do not run it.
</trajectory>
<response_shape>
Additive. On MySQL 8.0.12+ adding a column at the end of the table with a
*constant* default is metadata-only:

    SET SESSION lock_wait_timeout = 3;
    ALTER TABLE users ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'active',
      ALGORITHM=INSTANT;

Cost: metadata change, no rewrite of the 40M rows, brief metadata lock only.
Two traps worth stating: a volatile default (`UUID()`, `NOW()`) or a positional
`AFTER` clause before 8.0.29 forces a full rebuild instead; and instant-added
columns accumulate row versions, with a rebuild required past 64 of them.
Rollback: `DROP COLUMN` is also INSTANT on 8.0.29+, a rebuild before that.
Awaiting approval before anything is applied to production.
</response_shape>
</example>

<example>
<scenario>"Should we put these events in MongoDB or Cassandra?"</scenario>
<response_shape>
Out of scope in one line — this covers relational engines only — then the part
that is in scope: whether PostgreSQL or MySQL serves that access pattern and
volume, and what it would cost. Do not improvise a NoSQL data model to be
helpful; an unfounded answer about a store you cannot reason about precisely is
worth less than the boundary.
</response_shape>
</example>

</examples>

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
- Never echo credentials. Read them from the environment, `~/.pgpass`,
  `~/.my.cnf`, or the project's own secret tooling.
- Prefer a replica or a staging copy with production-like volume. Say so when
  the numbers you report came from a replica.

## Long audits and multi-session work

A whole-database review outlives one context window. Keep the state on disk so a
fresh session resumes instead of restarting:

- Collected evidence (snapshot output, plans, `pg_stat_statements` extracts) in
  files, referenced by path — never re-pasted into the conversation.
- Findings in a structured file as you confirm them (`findings.json`: object,
  severity, mechanism, evidence path, status), so ranking and status survive.
- Freeform progress notes for what is done, what is next, and which hypotheses
  were ruled out — the dead ends are the part a fresh session would otherwise
  re-walk.
- Deliver through `assets/report-template.md` once the findings stop changing.

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
