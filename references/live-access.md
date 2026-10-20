# Working against a live database

Load when a database is reachable — local container, staging, replica, or (with
care) production.

## Contents

- Safety rules
- Finding the connection
- Opening a read-only session
- What to collect before advising
- Reading the snapshot output
- Capturing a plan for the script
- Escalating to a write

## Safety rules

1. **Read-only by default.** Open every session read-only and with timeouts
   before running anything.
2. **`EXPLAIN ANALYZE` executes the statement.** For `INSERT`/`UPDATE`/`DELETE`,
   wrap it: `BEGIN; EXPLAIN (ANALYZE, BUFFERS) UPDATE …; ROLLBACK;`
3. **Nothing that writes, locks or kills runs without explicit approval** — DDL,
   DML, `VACUUM FULL`, `OPTIMIZE TABLE`, index creation,
   `pg_terminate_backend`/`KILL`. Show the exact statement and its lock level,
   then ask.
4. **Prefer a replica or a staging copy with production-like volume.** Say which
   one the numbers came from; a plan from a 1000-row dev table proves nothing.
5. **Never echo credentials** into the transcript or into a file. Read them from
   the environment, `~/.pgpass`, `~/.my.cnf`, or the project's secret tooling.
6. **Bound every query.** `LIMIT` on exploratory selects, `statement_timeout` on
   the session; catalog queries on huge schemas can themselves be slow.

## Finding the connection

Look, in order: `.env` / `.env.example`, `docker-compose.yml`, `config/*.php`,
`settings.py`, `knexfile.ts`, `ormconfig`, `alembic.ini`, CI secrets listings,
`~/.pgpass`, `~/.my.cnf`. Prefer an existing read-only role if the project has
one. If the only reachable database is production, say so and get approval for
the session before connecting.

## Opening a read-only session

```bash
# PostgreSQL — timeouts and read-only enforced server-side for this session
psql "$DATABASE_URL" -X -v ON_ERROR_STOP=1 \
  -c "SET default_transaction_read_only = on;
      SET statement_timeout = '30s';
      SET lock_timeout = '2s';
      SET idle_in_transaction_session_timeout = '10s';"
```

```bash
# MySQL
mysql --defaults-file=~/.my.cnf -e "
  SET SESSION TRANSACTION READ ONLY;
  SET SESSION max_execution_time = 30000;
  SET SESSION lock_wait_timeout = 3;"
```

For scripted output that is easy to parse: `psql -XAt -F $'\t'` or
`mysql -N -B`.

## What to collect before advising

Run the bundled packs rather than inventing catalog queries:

```bash
psql "$DATABASE_URL" -X -f assets/pg_snapshot.sql > /tmp/pg_snapshot.txt
mysql --defaults-file=~/.my.cnf db < assets/mysql_snapshot.sql > /tmp/my_snapshot.txt
```

Each pack gathers, in one pass: version and key settings, database and table
sizes, row-count estimates, index inventory with sizes and scan counts, unused
and redundant indexes, foreign keys missing a child index, dead-tuple / bloat
indicators, long-running and blocking sessions, and the top statements by total
time. That is the evidence base for every recommendation that follows.

Both packs are read-only and safe on a replica. `pg_stat_statements` and the
MySQL `sys` schema must be installed; the packs degrade gracefully with a notice
if they are not.

## Reading the snapshot output

- **Settings first.** `shared_buffers`, `work_mem`, `random_page_cost`,
  `effective_cache_size`, `max_connections` (PostgreSQL);
  `innodb_buffer_pool_size`, `innodb_flush_log_at_trx_commit`, `sort_buffer_size`
  (MySQL). A default `random_page_cost = 4` on SSD explains a lot of "the planner
  ignores my index" reports before you look at a single query.
- **Size before cleverness.** If the working set fits in RAM, I/O-shaped advice
  is noise; if the hot table is 40x the buffer pool, index size *is* the problem.
- **Statements by total time**, not by worst case — that is where capacity goes.
- **Dead tuples and the oldest transaction** tell you whether you are looking at
  a query problem or a vacuum problem.
- **Unused indexes** are write tax; check replicas before proposing a drop.

## Capturing a plan for the script

```bash
psql "$DATABASE_URL" -X -At -c \
  "EXPLAIN (ANALYZE, BUFFERS, SETTINGS, FORMAT JSON) SELECT …" > /tmp/plan.json
python3 scripts/explain_audit.py /tmp/plan.json
```

```bash
mysql -N -B -e "EXPLAIN FORMAT=JSON SELECT …" db > /tmp/plan.json
python3 scripts/explain_audit.py /tmp/plan.json      # MySQL subset of checks
```

Run the query twice and report the second run when you want the warm-cache
number, or report both — a cold first run and a warm second run answer different
questions.

## Escalating to a write

When a change is actually required, present it as a block the user can approve
as a unit:

```
Statement:  CREATE INDEX CONCURRENTLY idx_orders_tenant_created ON orders (tenant_id, created_at);
Lock:       ShareUpdateExclusive (writes continue; blocks other DDL on the table)
Expected:   ~4 min on 52M rows; +1.8 GB disk
Rollback:   DROP INDEX CONCURRENTLY idx_orders_tenant_created;
Risk:       build failure leaves an invalid index — verify pg_index.indisvalid after
```

Then wait for an explicit yes. Apply one change at a time and re-measure after
each.
