# Engine internals, transactions and locking

Load for any question about transactions, isolation, deadlocks, lock waits,
bloat, or "why does this behave differently on MySQL".

## Contents

- Why the engine matters
- The physical write path (ACID)
- MVCC: heap versions vs undo log
- Bloat, vacuum, and the idle-transaction hazard
- The five concurrency anomalies
- Isolation levels in practice
- Locking mechanics per engine
- Lost updates
- Write skew and SSI
- Deadlocks
- Queue patterns: SKIP LOCKED
- Connection pools and lock-holding

## Why the engine matters

PostgreSQL and MySQL/InnoDB share SQL syntax and make opposite choices
underneath: heap vs clustered storage, in-heap row versions vs undo log,
snapshot isolation vs gap locking. Advice that ignores the engine is wrong for
one of them roughly half the time. Establish the engine first, every time.

## The physical write path (ACID)

1. **Atomicity via logging.** The engine never writes the data file first. It
   modifies the page in the buffer pool (marking it dirty) and appends a log
   record — the WAL (PostgreSQL) or redo log (InnoDB) — before acknowledging the
   commit. Recovery replays the log forward and undoes partial transactions via
   the undo log (InnoDB) or commit status bits in `pg_xact` (PostgreSQL).
2. **Consistency via invariants.** `NOT NULL`, foreign keys, unique indexes and
   `CHECK` are the only guarantees that survive concurrency bugs. PostgreSQL
   deferred constraints move the check to `COMMIT`, which is what makes transient
   violations inside a batch legal.
3. **Isolation via snapshots.** Under MVCC a reader takes no shared locks; it
   reads the row version matching its snapshot. Writers do not block readers;
   readers do not block writers.
4. **Durability via fsync.** A commit is durable only once the log record is
   `fsync`'d to non-volatile storage. (PostgreSQL `synchronous_commit`,
   InnoDB `innodb_flush_log_at_trx_commit` — both trade durability for throughput
   when relaxed.)

## MVCC: heap versions vs undo log

**PostgreSQL — in-heap versioning.** Every `UPDATE` is physically an `INSERT` of
a new tuple plus marking the old one expired. Both versions live in the table's
heap pages; each row header carries `xmin` (inserting transaction) and `xmax`
(deleting/replacing transaction). Dead tuples are reclaimed later by autovacuum.
Consequence: updates bloat the table *and every index on it*, and an index entry
exists per row version (HOT updates avoid this only when no indexed column
changed and the page has room).

**MySQL InnoDB — undo log segments.** An `UPDATE` modifies the row in place in
the clustered leaf page; the previous image goes to the undo log, and the row
keeps a 7-byte `roll_ptr` back to it. Secondary indexes point at the primary key,
so they are untouched unless the indexed column itself changed. Consequence:
updates are cheaper for secondary indexes, but readers of old snapshots must walk
undo chains.

## Bloat, vacuum, and the idle-transaction hazard

PostgreSQL cannot remove any tuple that might still be visible to the oldest open
snapshot. One connection sitting `idle in transaction` therefore freezes cleanup
for the whole database: tables and indexes grow, plans degrade, disk fills.

```sql
-- PostgreSQL: who is holding the horizon open
SELECT pid, now() - xact_start AS duration, state, wait_event_type, left(query, 80)
FROM pg_stat_activity
WHERE state <> 'idle'
ORDER BY duration DESC;

-- and the dead-tuple picture
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum
FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 20;
```

```sql
-- MySQL: look for "History list length" — >100,000 means purge is far behind
SHOW ENGINE INNODB STATUS\G
SELECT * FROM information_schema.innodb_trx ORDER BY trx_started LIMIT 10;
```

Fixes: set `idle_in_transaction_session_timeout`, never hold a transaction open
across an HTTP call or a user think-time, tune autovacuum per hot table
(`autovacuum_vacuum_scale_factor` down to 0.01–0.05 on large churning tables),
and never let an ORM open a transaction at request start "just in case".

## The five concurrency anomalies

1. **Dirty read** — reading another transaction's uncommitted write.
2. **Non-repeatable read** — the same row read twice returns different values.
3. **Phantom read** — a range query re-run returns extra rows.
4. **Lost update** — two transactions read a value, compute, and write back;
   one write is silently overwritten.
5. **Write skew** — two transactions read an overlapping set, each checks an
   invariant, each modifies *different* rows, and together they break the
   invariant (the "at least one doctor on call" case).

The ANSI levels only speak about the first three. Lost updates and write skew are
where production bugs actually live.

## Isolation levels in practice

| Level | PostgreSQL | MySQL InnoDB | Prevents |
|---|---|---|---|
| Read Committed | statement-level snapshot; fresh view per query (**default**) | statement-level read view per query | dirty reads |
| Repeatable Read | transaction-level snapshot at the first non-DML statement; no gap locks | transaction-level snapshot at first read + gap/next-key locks on writes (**default**) | + non-repeatable and phantom reads |
| Serializable | Serializable Snapshot Isolation (SSI): SIREAD tracking, no read blocking, aborts on dependency cycles | strict 2PL: implicit shared locks on plain `SELECT` | + write skew |

Note the default differs: Read Committed in PostgreSQL, Repeatable Read in MySQL.
Code ported between them changes behaviour silently.

## Locking mechanics per engine

**InnoDB** uses record, gap and next-key locks. A *record lock* locks an index
record; a *gap lock* locks the space between records; a *next-key lock* is the
union `(previous_key, current_key]`.

```sql
-- InnoDB, REPEATABLE READ
SELECT * FROM inventory WHERE item_id BETWEEN 10 AND 20 FOR UPDATE;
-- Another session inserting item_id = 15 now WAITS until this commits.
```

**PostgreSQL** has no gap locks. Under Repeatable Read the second transaction's
insert succeeds and simply is not visible to the first. But if the first tries to
update a row that another committed after its snapshot began:

```
ERROR: could not serialize access due to concurrent update
```

The application must catch this and retry. Same isolation name, completely
different failure mode — plan the retry logic for the engine you actually run.

Useful lock introspection:

```sql
-- PostgreSQL: who blocks whom
SELECT blocked.pid AS blocked_pid, blocked.query AS blocked_query,
       blocking.pid AS blocking_pid, blocking.query AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0;
```

```sql
-- MySQL 8
SELECT * FROM performance_schema.data_lock_waits;
SELECT * FROM sys.innodb_lock_waits;
```

## Lost updates

```sql
-- Anti-pattern under Read Committed
SELECT stock FROM items WHERE id = 1;    -- both sessions read 10
UPDATE items SET stock = 8 WHERE id = 1; -- computed in the application
-- the second write overwrites the first; inventory silently drifts
```

Two correct patterns:

```sql
-- 1. Atomic single statement (preferred: smallest lock footprint)
UPDATE items SET stock = stock - 2 WHERE id = 1 AND stock >= 2;
-- check affected rows: 0 means insufficient stock, not success

-- 2. Pessimistic lock, when cross-table validation must happen in between
BEGIN;
SELECT stock FROM items WHERE id = 1 FOR UPDATE;
-- business validation here (no network calls!)
UPDATE items SET stock = stock - 2 WHERE id = 1;
COMMIT;
```

Optimistic locking (a `version` column, `UPDATE … WHERE version = :v`) is the
third option and the right one for long user-facing edit flows.

## Write skew and SSI

```sql
-- Both transactions see 2 doctors on call; both commit; zero remain.
BEGIN;
SELECT count(*) FROM shift WHERE on_call = true;   -- 2
UPDATE shift SET on_call = false WHERE doctor_id = 'Alice';
COMMIT;
```

- **PostgreSQL**: `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE`. SSI tracks
  read-write dependencies via SIREAD locks and aborts one transaction with
  `could not serialize access due to read/write dependencies among transactions`.
- **MySQL**: elevate to `SERIALIZABLE` (which takes shared locks on plain
  `SELECT`), or take explicit locks covering the predicate — which in practice
  means locking the rows *and* the gaps.
- **Either engine**: materialize the invariant into a row you can lock (a
  `on_call_count` row, a unique partial index, an `EXCLUDE` constraint).

**Never recommend `SERIALIZABLE` without a retry loop.** Serialization failures
are the mechanism, not an error: catch the SQLSTATE (`40001`, and `40P01` for
deadlocks), retry with exponential backoff and a cap, and make the transaction
body idempotent.

## Deadlocks

Two transactions each hold what the other needs; the engine's wait-for graph
detects the cycle and kills a victim (`40P01` / `ERROR 1213`).

The fix is deterministic lock ordering — always acquire in the same order,
everywhere:

```sql
BEGIN;
SELECT * FROM inventory WHERE item_id IN (14, 52) ORDER BY item_id FOR UPDATE;
UPDATE inventory SET stock = stock - 1 WHERE item_id = 14;
UPDATE inventory SET stock = stock - 1 WHERE item_id = 52;
COMMIT;
```

Also: keep transactions short, touch tables in a fixed order across the codebase,
avoid upgrading a shared lock to exclusive mid-transaction, and read
`SHOW ENGINE INNODB STATUS` → "LATEST DETECTED DEADLOCK" / the PostgreSQL log
entry to see the real cycle rather than guessing.

## Queue patterns: SKIP LOCKED

For worker queues, never poll with a plain `SELECT … FOR UPDATE` — workers
serialize behind each other.

```sql
-- PostgreSQL / MySQL 8
BEGIN;
SELECT id, payload FROM task_queue
WHERE status = 'QUEUED'
ORDER BY created_at
LIMIT 10
FOR UPDATE SKIP LOCKED;
-- … process, then mark done in the same transaction
COMMIT;
```

`NOWAIT` is the other variant: fail immediately rather than wait. Pair either
with a partial index on the queued states.

## Connection pools and lock-holding

A transaction holding row locks while waiting on an external HTTP call converts
one slow dependency into total lock contention and pool exhaustion. Rules:

- No network calls inside a transaction. Ever.
- Keep transactions to the smallest set of statements that must be atomic.
- Size the pool below what the database can actually serve concurrently
  (`max_connections` is not a target; use PgBouncer/ProxySQL for high counts).
- Set `statement_timeout`, `idle_in_transaction_session_timeout` and
  `lock_timeout` at the role level so a forgotten transaction cannot wedge the
  system.
