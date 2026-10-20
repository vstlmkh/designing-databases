# designing-databases

An [Agent Skill](https://code.claude.com/docs/en/skills) for designing, auditing
and tuning relational databases on **PostgreSQL** and **MySQL/InnoDB**.

Generic SQL advice ("add an index", "normalize it") misses how relational
engines actually fail — a page split, a lock queue, a stale visibility map, a
cardinality misestimate — and the two engines diverge exactly where it matters.
This skill works from evidence (the real schema, the real plan, the real
statistics) and names the engine mechanism behind every recommendation.

## What it covers

- **Schema design** — normalization, primary keys, column types, constraints,
  JSON vs columns, multi-tenancy, soft deletes.
- **Index strategy** — B+tree mechanics, ESR ordering, covering / partial /
  expression indexes, GIN / BRIN / hash, redundancy and bloat.
- **Query tuning** — reading `EXPLAIN (ANALYZE, BUFFERS)` plans, join
  algorithms, sargability, statistics and memory settings.
- **Transactions and concurrency** — isolation levels, locking, deadlocks,
  lock waits, the five concurrency anomalies, bloat and replication lag.
- **Zero-downtime migrations** — expand/contract, lock-safe DDL per engine, and
  the specific commands and traps for Alembic, Django, Laravel, Doctrine,
  Prisma, TypeORM, Knex, Drizzle, Flyway, Liquibase, Atlas and golang-migrate.

Out of scope: NoSQL stores. The skill never applies DDL without explicit
approval.

## Install

Clone into your skills directory — personal (`~/.claude/skills/`) or project
(`.claude/skills/`):

```bash
git clone git@github.com:vstlmkh/designing-databases.git \
  ~/.claude/skills/designing-databases
```

The skill activates on its own when a conversation turns to schema design,
indexes, slow queries or migrations — including when the database is never
named and the user only says "this endpoint got slow" or "the deploy locked
everything".

## Bundled scripts

Standard-library Python 3. No install, no network. Each exits `0` when clean,
`1` on an ERROR-level finding and `2` on bad input, so they also work as a CI
gate.

```bash
# Structural audit of DDL (pg_dump --schema-only, mysqldump --no-data, migrations)
python3 scripts/schema_audit.py schema.sql --engine postgres

# Rank the real bottlenecks in an execution plan (loop math, misestimates, spills)
psql -XAt -c "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT …" > plan.json
python3 scripts/explain_audit.py plan.json

# Lint migrations for locking, rewriting and deploy-breaking operations
python3 scripts/migration_lint.py db/migrations --engine postgres
```

## Layout

```
SKILL.md                      entry point: routing, the three workflows, gotchas
references/
  schema-design.md            normalization, keys, types, constraints
  indexing.md                 index mechanics, ESR, maintenance runbook
  query-tuning.md             plan nodes, joins, sargability, statistics
  engines.md                  heap/MVCC vs clustered index, isolation, locking
  migrations.md               expand/contract, lock-safe DDL, per-framework notes
  live-access.md              read-only connection recipes and query packs
scripts/                      schema_audit · explain_audit · migration_lint
assets/                       catalog snapshot SQL, report template
evals/                        trigger, output and negative-case evaluations
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE.md](LICENSE.md).
