# Migrations

Load in Workflow C — writing, reviewing, or de-risking a schema change.

## Contents

- The rule that makes migrations safe
- Expand / contract
- Lock-safe DDL: PostgreSQL
- Lock-safe DDL: MySQL / InnoDB
- Online schema change tools
- Backfills
- Engine-agnostic runners: Flyway, Liquibase, Atlas, golang-migrate
- Python: Alembic, Django
- Node/TypeScript: Prisma, TypeORM, Knex, Drizzle
- PHP: Laravel, Doctrine
- Review checklist

## The rule that makes migrations safe

**During a deploy, the old code and the new schema run at the same time.** Every
migration must be compatible with the application version already running, and
every application version must be compatible with the schema before and after.
That single constraint produces expand/contract, and it is why "just rename the
column" is never a one-step change.

Second rule: a migration must never hold a lock longer than the slowest query on
the table. Always set a lock timeout and retry rather than queue.

## Expand / contract

| Change | Steps |
|---|---|
| Rename a column | add new → write both → backfill → read new → stop writing old → drop old |
| Change a type | add new column of the new type → dual-write → backfill in batches → switch reads → drop old |
| Add a required column | add nullable → backfill → add `NOT NULL` via validated CHECK → set default |
| Drop a column | stop reading in code → deploy → stop writing → deploy → drop |
| Split/merge tables | create new → dual-write → backfill → switch reads → retire old |

Each arrow is a separate deploy. The migration that drops something must land at
least one release after the code that stopped using it.

## Lock-safe DDL: PostgreSQL

Most `ALTER TABLE` forms take `ACCESS EXCLUSIVE`, which blocks reads *and*
writes. The danger is not the duration of the DDL but the lock queue: a DDL
waiting behind one long query blocks every statement that arrives after it.

```sql
SET lock_timeout = '3s';        -- fail fast instead of queueing; retry in a loop
SET statement_timeout = '0';    -- but let the work itself finish
```

| Operation | Cost | Safe form |
|---|---|---|
| `ADD COLUMN` (no default / constant default) | metadata only (PG 11+) | direct |
| `ADD COLUMN … DEFAULT now()` / `gen_random_uuid()` | full table rewrite (volatile) | add nullable → backfill → set default |
| `SET NOT NULL` | full scan under ACCESS EXCLUSIVE | `ADD CONSTRAINT … CHECK (col IS NOT NULL) NOT VALID` → `VALIDATE CONSTRAINT` → `SET NOT NULL` (scan skipped, PG 12+) |
| `ADD FOREIGN KEY` / `ADD CHECK` | scan + blocking | add `NOT VALID`, then `VALIDATE CONSTRAINT` (SHARE UPDATE EXCLUSIVE, writes allowed) |
| `ALTER COLUMN … TYPE` | rewrites table + indexes | expand/contract; exception: binary-coercible types (e.g. `varchar(50)`→`varchar(100)`) |
| `CREATE INDEX` | blocks writes for the build | `CREATE INDEX CONCURRENTLY` (outside a transaction) |
| `DROP INDEX` | brief ACCESS EXCLUSIVE | `DROP INDEX CONCURRENTLY` |
| Add a primary key | blocking | `CREATE UNIQUE INDEX CONCURRENTLY`, then `ADD CONSTRAINT … PRIMARY KEY USING INDEX` |
| `SET DEFAULT` / `DROP DEFAULT` | metadata only | direct |

`CREATE INDEX CONCURRENTLY` cannot run inside a transaction block, takes roughly
twice as long, and on failure leaves an invalid index that still costs writes:

```sql
SELECT relname, indisvalid FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid WHERE NOT indisvalid;
-- then: DROP INDEX CONCURRENTLY <name>;  and retry
```

## Lock-safe DDL: MySQL / InnoDB

State the algorithm explicitly. If the server cannot honour it, the statement
fails — which is far better than silently copying a 300 GB table.

```sql
ALTER TABLE orders ADD COLUMN note VARCHAR(255), ALGORITHM=INSTANT;
ALTER TABLE orders ADD INDEX idx_orders_status (status), ALGORITHM=INPLACE, LOCK=NONE;
SET SESSION lock_wait_timeout = 3;   -- metadata-lock guard
```

| Operation | Algorithm | Notes |
|---|---|---|
| Add / drop / rename column | `INSTANT` (MySQL 8.0.12+ / 8.0.29 for drop) | capped at 64 row versions before a rebuild is required; not for COMPRESSED tables |
| Set / drop column default | `INSTANT` | data-dictionary only |
| Add / drop / rename index | `INPLACE`, `LOCK=NONE` | builds without blocking DML |
| Extend `VARCHAR` within the same length-byte class (≤255 or ≥256) | `INPLACE`, `LOCK=NONE` | crossing 255→256 needs `COPY` |
| `MODIFY … NOT NULL` | `INPLACE` | requires strict SQL mode; fails if NULLs exist |
| Add primary key | `INPLACE` | rebuilds, but online |
| Change column data type | `COPY` | blocks DML — use gh-ost / pt-osc |
| Shrink `VARCHAR`, drop primary key, add `STORED` generated column | `COPY` | blocks DML |
| Add foreign key | `INPLACE` only with `foreign_key_checks=0` | otherwise `COPY` |

Even an `INSTANT` DDL needs a metadata lock, which waits for open transactions on
the table and blocks everything behind it meanwhile. Check
`SHOW PROCESSLIST` / `performance_schema.metadata_locks` before firing one.

## Online schema change tools

When InnoDB forces a `COPY`, or the table is too large for a single ALTER:

```bash
# gh-ost: binlog-based, no triggers, pausable and throttleable
gh-ost --host=replica.example.com --database=shop --table=orders \
  --alter="MODIFY COLUMN total DECIMAL(14,2) NOT NULL" \
  --max-load=Threads_running=25 --critical-load=Threads_running=100 \
  --chunk-size=1000 --throttle-control-replicas=r1,r2 \
  --postpone-cut-over-flag-file=/tmp/cutover.flag \
  --execute        # without --execute it is a dry run — always dry-run first
```

```bash
# pt-online-schema-change: trigger-based; simpler, but triggers add write overhead
pt-online-schema-change --alter "ADD INDEX idx_status (status)" \
  D=shop,t=orders --max-lag=1 --dry-run
```

PostgreSQL rarely needs these (concurrent index builds plus `NOT VALID`
constraints cover most cases); for a true table rewrite without downtime use
logical replication into a new table, or `pg_repack` for bloat.

## Backfills

Never one statement over the whole table: PostgreSQL doubles the heap and stalls
autovacuum; InnoDB grows the undo log until commit, and a rollback can take
longer than the update.

```sql
-- Batch by primary key, one transaction per chunk, with a pause between chunks
UPDATE orders SET status_code = status
WHERE id BETWEEN :lo AND :hi AND status_code IS NULL;
```

Rules: bound each batch (1k–10k rows), commit per batch, make it resumable and
idempotent (`WHERE new_col IS NULL`), log progress, throttle on replication lag,
and run it as a job — not inside the migration that holds the DDL lock.

## Engine-agnostic runners: Flyway, Liquibase, Atlas, golang-migrate

**Flyway** — versioned SQL files, applied in version order, checksummed in
`flyway_schema_history`.

```bash
flyway migrate       # apply pending
flyway info          # what is applied / pending
flyway validate      # checksums vs history
flyway repair        # realign checksums after a legitimate file change
flyway baseline      # adopt an existing database at a version
```

Naming: `V2__add_orders_index.sql` (versioned), `R__refresh_views.sql`
(repeatable, re-applied whenever the checksum changes), `U2__…` (undo, paid
editions). **Never edit an applied migration** — the checksum mismatch fails
every subsequent run on every environment; ship a new version instead.
Put `CREATE INDEX CONCURRENTLY` in a script marked non-transactional
(`executeInTransaction=false` in the script's `.conf`).

**Liquibase** — changelog of changesets, identified by `author:id:filepath` and
tracked in `DATABASECHANGELOG` with a checksum.

```bash
liquibase update                 # apply pending changesets
liquibase status --verbose
liquibase update-sql             # print SQL without applying — review gate
liquibase rollback <tag> | rollback-count 1
liquibase changelog-sync         # mark changesets applied (object already exists)
liquibase update-testing-rollback
```

Write an explicit `<rollback>` for anything Liquibase cannot auto-reverse, use
`contexts`/`labels` to scope environment-specific changesets, and preconditions
to make a changeset self-checking. Changing an applied changeset's body breaks
the checksum — add a new changeset.

**Atlas** — declarative schema or versioned migrations, diffed against a
throwaway *dev database*.

```bash
atlas migrate diff add_orders_index \
  --dir "file://migrations" --to "file://schema.hcl" --dev-url "docker://postgres/16/dev"
atlas migrate lint --dir "file://migrations" --dev-url "docker://postgres/16/dev" --latest 1
atlas migrate hash          # after any manual edit to a migration file
atlas migrate apply --url "$DATABASE_URL"
```

`atlas migrate lint` is a genuine CI gate: it flags destructive and
lock-dangerous changes. Atlas can emit concurrent index builds via the
`diff.concurrent_index` block in `atlas.hcl`. Always `migrate hash` after
editing a file by hand, or the directory integrity check fails.

**golang-migrate** — paired `up`/`down` SQL files.

```bash
migrate create -ext sql -dir db/migrations -seq add_orders_index
migrate -path db/migrations -database "$DATABASE_URL" up
migrate -path db/migrations -database "$DATABASE_URL" down 1
migrate -path db/migrations -database "$DATABASE_URL" force 7   # clear a dirty state
```

A failed migration leaves the database **dirty**; nothing else runs until you
inspect what actually applied and `force` the correct version. Statements run
inside a transaction by default — for `CREATE INDEX CONCURRENTLY`, disable it for
that migration (`x-no-transaction` / the driver's `x-multi-statement` settings).

## Python: Alembic, Django

**Alembic**

```bash
alembic revision --autogenerate -m "add orders status index"
alembic upgrade head
alembic downgrade -1
alembic current / history / heads
alembic merge heads          # after two branches both created a head
```

Autogenerate detects added/removed tables and columns, nullability, basic index
and FK changes, and (by default) type changes. It does **not** detect table or
column renames (they come out as drop+add — hand-edit them), unnamed
constraints, or server-default changes unless `compare_server_default=True`.
Always read the generated file before committing it.

Concurrent index build (otherwise Alembic wraps the migration in a transaction):

```python
def upgrade():
    with op.get_context().autocommit_block():
        op.create_index("idx_orders_status", "orders", ["status"],
                        postgresql_concurrently=True)
```

Use `op.batch_alter_table` for SQLite, and `op.execute` for anything the DSL
cannot express.

**Django**

```bash
python manage.py makemigrations app
python manage.py sqlmigrate app 0042      # see the SQL before it runs — review gate
python manage.py migrate
python manage.py migrate app 0041          # revert to a previous migration
```

```python
from django.contrib.postgres.operations import AddIndexConcurrently

class Migration(migrations.Migration):
    atomic = False          # REQUIRED: CONCURRENTLY cannot run in a transaction
    operations = [
        AddIndexConcurrently("event", models.Index(fields=["tenant_id", "created_at"],
                                                   name="ev_tenant_created_idx")),
    ]
```

Keep schema and data migrations in separate files; give every `RunPython` a
`reverse_code` (or `migrations.RunPython.noop`); use the historical model from
`apps.get_model(...)`, never the imported model class. On MySQL, DDL commits
implicitly, so `atomic = True` buys you nothing there.

## Node/TypeScript: Prisma, TypeORM, Knex, Drizzle

**Prisma**

```bash
npx prisma migrate dev --name add_orders_index   # DEV ONLY: needs a shadow database
npx prisma migrate deploy                        # production: applies pending, no drift check
npx prisma migrate status
npx prisma migrate resolve --applied <migration> # repair a failed production migration
npx prisma migrate diff --from-schema-datamodel … --to-schema-datasource …
npx prisma db push                               # prototyping only: no migration file
```

The shadow database (created and dropped by `migrate dev`) requires `CREATEDB`
on PostgreSQL; on managed clouds that forbid it, provision one and set
`shadowDatabaseUrl`. Never run `migrate dev`, `db push` or `migrate reset`
against production. To customize DDL (concurrent index, `NOT VALID`), generate
the migration, then edit the SQL file before applying it.

**TypeORM**

```bash
typeorm migration:generate -d src/data-source.ts src/migrations/AddOrdersIndex
typeorm migration:run -d src/data-source.ts
typeorm migration:revert -d src/data-source.ts
```

`synchronize: true` silently drops and recreates columns to match entities —
acceptable only in a throwaway local database, never anywhere else. Generated
migrations are a starting point: they often emit a drop+add for a rename.

**Knex**

```bash
knex migrate:make add_orders_index
knex migrate:latest
knex migrate:rollback --all
knex migrate:status
```

Knex wraps each migration in a transaction; disable it for concurrent index
builds with `exports.config = { transaction: false }`.

**Drizzle**

```bash
drizzle-kit generate   # diff TS schema -> SQL migration file
drizzle-kit migrate    # apply pending files
drizzle-kit push       # prototyping only: apply directly, no file
drizzle-kit check      # detect conflicting/corrupt migration metadata
```

Use `generate` + `migrate` in production so the SQL is reviewable; `push` is for
local iteration. Rename detection is interactive at generate time — answer it, or
you get a drop+add that loses data.

## PHP: Laravel, Doctrine

**Laravel**

```bash
php artisan make:migration add_status_index_to_orders_table
php artisan migrate --pretend     # print the SQL without running it — review gate
php artisan migrate
php artisan migrate:rollback --step=1
php artisan migrate:status
```

```php
Schema::table('orders', function (Blueprint $table) {
    $table->string('status_code', 20)->nullable();   // additive, nullable first
});
// Concurrent index / NOT VALID constraints need raw SQL:
DB::statement('CREATE INDEX CONCURRENTLY idx_orders_status ON orders (status)');
```

On MySQL, DDL commits implicitly: a migration that mixes schema changes with a
backfill cannot roll back the DDL if the backfill fails, leaving the database
half-migrated. Keep them in separate migrations. `->change()` on a column relies
on doctrine/dbal and can silently drop attributes it does not know about — review
the `--pretend` output. Never create an index inside a loop over records.

**Doctrine Migrations (Symfony)**

```bash
php bin/console doctrine:migrations:diff        # generate from entity mapping diff
php bin/console doctrine:migrations:migrate
php bin/console doctrine:migrations:migrate prev
php bin/console doctrine:migrations:status
php bin/console doctrine:migrations:execute --up 'DoctrineMigrations\Version20260101120000'
```

Always review a `diff`: it round-trips the whole mapping and will happily emit
drops for tables it does not know about (`schema_filter` prevents this). Override
`isTransactional(): bool { return false; }` for `CREATE INDEX CONCURRENTLY`. Put
data changes in `postUp()` or a separate command, not in `up()` alongside DDL.

## Review checklist

Run `python3 scripts/migration_lint.py <dir> --engine <engine>` first, then
judge what it cannot:

- [ ] Is every statement compatible with the currently-deployed application code?
- [ ] Is anything destructive or renaming? If so, is it split across releases?
- [ ] What lock does each statement take, and for how long on production volume?
- [ ] Is there a `lock_timeout` / `lock_wait_timeout` guard?
- [ ] Are index builds concurrent / `LOCK=NONE`?
- [ ] Are constraints added `NOT VALID` and validated separately (PostgreSQL)?
- [ ] Are backfills batched, resumable, and outside the DDL migration?
- [ ] Does the down path exist and actually work — and is data loss acceptable?
- [ ] Has it been run against a production-sized copy, with timings recorded?
- [ ] Does it change replication-sensitive objects (large rewrites → lag)?
