# Schema design

Load in Workflow A — modelling new tables, or reviewing an existing model.

## Contents

- Normalize first, denormalize with a receipt
- Primary keys: the physical consequences
- Foreign keys
- Column types that cause incidents
- Constraints are the cheapest code you will ever write
- Dynamic attributes: JSON vs EAV vs columns
- Multi-tenancy
- Soft deletes
- Time, money, enums, text
- Naming conventions
- Antipattern checklist

## Normalize first, denormalize with a receipt

Target 3NF for OLTP: every non-key attribute depends on the key, the whole key,
and nothing but the key. Violations show up as update anomalies — the same fact
stored twice, drifting apart.

```sql
-- Anti-pattern: a customer-level fact stored on the line item
CREATE TABLE order_items (
    order_id          INT,
    item_id           INT,
    customer_discount NUMERIC(5,2),   -- belongs to the customer or the order
    price             NUMERIC(10,2)
);

-- 3NF
CREATE TABLE orders (
    order_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id      BIGINT      NOT NULL REFERENCES customers(customer_id),
    applied_discount NUMERIC(5,2) NOT NULL DEFAULT 0.00,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    order_id   BIGINT        NOT NULL REFERENCES orders(order_id),
    item_id    BIGINT        NOT NULL REFERENCES items(item_id),
    unit_price NUMERIC(10,2) NOT NULL,          -- intentional snapshot: the price
    quantity   INT           NOT NULL CHECK (quantity > 0),  -- at time of sale
    PRIMARY KEY (order_id, item_id)
);
```

Note the one deliberate denormalization above: `unit_price` is copied because the
business needs the price *as of* the sale, not today's price. That is the test —
a denormalized column is acceptable when it is a historical snapshot or when a
named query cannot meet its latency budget otherwise. Write the reason in a
`COMMENT ON COLUMN`, and state what keeps the copy in sync.

## Primary keys: the physical consequences

The key choice is a storage-layout decision, not an aesthetic one.

| Key | Bytes | Monotonic | InnoDB | PostgreSQL |
|---|---|---|---|---|
| `BIGINT` identity | 8 | yes | Optimal: appends to the rightmost leaf, no random page splits | Optimal: sequential B-tree inserts, high cache hit ratio |
| UUIDv4 as `CHAR(36)`/`VARCHAR(36)` | 36 | no | Catastrophic: constant page splits, every secondary index ~4.5x larger | Poor: heavy index bloat, cache misses once the index exceeds RAM |
| UUIDv4 as `BINARY(16)`/`uuid` | 16 | no | Bad: page splits continue, smaller index overhead | Moderate: index misses cache, heap inserts stay fast |
| UUIDv7 / ULID as `BINARY(16)`/`uuid` | 16 | yes | Good: sequential leaf appends, compact secondary keys | Good: localized B-tree inserts, high cache locality |

Why it matters physically:

- **InnoDB: the primary key *is* the table.** The clustered index leaf page holds
  the full row. A random key lands on an arbitrary page; when that 16 KB page is
  full InnoDB splits it, copies ~half the records, and rewires parent pointers.
  Pages stabilize at 50–67% fill, the index doubles in size, and the buffer pool
  thrashes. Every secondary index leaf also stores the PK value as its row
  pointer, so PK width multiplies across all of them.
- **PostgreSQL: the table is an unordered heap**, so random keys do not split
  *data* pages — but they do scatter writes across the PK B-tree. Once that index
  exceeds `shared_buffers`, each insert reads an uncached page from storage.

Default to `BIGINT GENERATED ALWAYS AS IDENTITY` (PostgreSQL) or
`BIGINT AUTO_INCREMENT` (MySQL). Use UUIDv7/ULID when IDs are exposed to clients
and must not be enumerable, stored as `uuid` (PostgreSQL) or `BINARY(16)`
(MySQL) — never as text. Natural keys (email, SKU) make poor primary keys because
they change and because every foreign key copies them.

Composite primary keys are right for pure join tables and for child tables that
only exist within a parent (`(order_id, item_id)`), and they cluster the children
of one parent together — a real win in InnoDB.

## Foreign keys

- Declare them. "The application enforces it" means the application enforces it
  until the one code path that doesn't.
- **Index the child column.** PostgreSQL never creates it for you: without it, a
  parent `DELETE` or key `UPDATE` scans the whole child table while holding
  locks — a classic source of connection-pool exhaustion. InnoDB *requires* an
  index and silently creates a single-column one named after the constraint if
  none exists — which is better than nothing but is rarely the index the queries
  want, and it disappears as a concern only while the FK is declared. Columns
  that are foreign keys *in spirit* but undeclared (common after
  `foreign_key_checks=0` imports or ORM-managed relations) get nothing on either
  engine.
- Match types exactly, including signedness and width. An `INT` child pointing at
  a `BIGINT` parent overflows and can force an implicit cast that kills index
  joins.
- Pick `ON DELETE` deliberately: `RESTRICT` (default, safest), `CASCADE` (only
  when the child has no independent meaning), `SET NULL` (only when the column is
  nullable by design). Cascades fire row-by-row and can turn one delete into a
  lock storm.

## Column types that cause incidents

| Need | Use | Not |
|---|---|---|
| Money | `NUMERIC(p,s)` / `DECIMAL(p,s)`, or integer minor units | `FLOAT`, `DOUBLE`, `REAL` — 0.1 is not representable, sums drift |
| Instant in time | `TIMESTAMPTZ` (PostgreSQL), `TIMESTAMP` or UTC `DATETIME` (MySQL) | `timestamp without time zone`, local-time strings |
| Date only | `DATE` | `VARCHAR(10)` |
| Enumerated state | lookup table + FK, or `CHECK (col IN (…))` | native `ENUM` — every new value is a migration, MySQL reorders silently |
| Boolean | `BOOLEAN` / `TINYINT(1)` | `VARCHAR('Y'/'N')` |
| Text of bounded length | `VARCHAR(n)` with a real business bound | unbounded `TEXT` in MySQL index keys and temp tables |
| Identifier from elsewhere | the same type as the source | `VARCHAR` holding digits |

MySQL specifics worth knowing: `utf8mb4` costs up to 4 bytes per character, so a
`VARCHAR(255)` index key is 1020 bytes against InnoDB's 3072-byte limit — three
such columns in one composite index is the ceiling. `DATETIME` stores no zone and
does no conversion; `TIMESTAMP` converts to/from UTC using the session zone and
ends in 2038.

## Constraints are the cheapest code you will ever write

- `NOT NULL` by default. Nullability is a business statement; `NULL` propagates
  through comparisons and silently changes aggregate results.
- `CHECK` for ranges, non-empty strings, and state machines.
- `UNIQUE` for every real-world identity (email, slug, external ID). A partial
  unique index expresses "unique among live rows":
  `CREATE UNIQUE INDEX ON users (email) WHERE deleted_at IS NULL;`
- PostgreSQL `DEFERRABLE INITIALLY DEFERRED` lets a batch violate a constraint
  transiently and checks it at `COMMIT` — useful for circular references and
  reordering operations.
- PostgreSQL `EXCLUDE USING gist (room WITH =, during WITH &&)` enforces
  non-overlapping ranges (bookings, schedules) — no application locking needed.

## Dynamic attributes: JSON vs EAV vs columns

Entity-Attribute-Value (`entity_id, attribute_name, attribute_value`) looks like
flexibility and destroys relational integrity: foreign keys cannot target generic
text, numbers and dates sort as strings, and reading one entity with ten
attributes takes ten self-joins.

When fields are genuinely user-defined, use the engine's semi-structured type:

```sql
CREATE TABLE customer_profiles (
    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Index the paths that are actually queried, not the whole document
CREATE INDEX idx_profiles_tier ON customer_profiles ((metadata->>'tier'));
CREATE INDEX idx_profiles_gin  ON customer_profiles USING GIN (metadata jsonb_path_ops);

-- Keep invariants even inside the document
ALTER TABLE customer_profiles
  ADD CONSTRAINT profile_tier_valid
  CHECK (metadata->>'tier' IN ('free','pro','enterprise'));
```

Rule of thumb: if a field is queried, sorted, or joined on by more than one
feature, promote it to a real column. JSON is for the long tail.

## Multi-tenancy

Three shapes, in increasing isolation and operational cost:

1. **Shared tables with `tenant_id`** — one schema, `tenant_id` as the leading
   column of every index and primary key, and row-level security (PostgreSQL
   `CREATE POLICY`) or a mandatory application filter. Cheapest; the risk is a
   missing `WHERE tenant_id`.
2. **Schema per tenant** — isolation without separate servers; migrations must
   loop over schemas, and thousands of schemas strain the catalog.
3. **Database per tenant** — strongest isolation, heaviest operations.

For (1), make `tenant_id` the first column of composite indexes — it is always an
equality predicate, which is exactly where the ESR rule puts it.

## Soft deletes

A `deleted_at` column means every query must filter it and every index carries
dead rows. If you adopt it:

- Make the hot-path indexes partial: `… WHERE deleted_at IS NULL`.
- Make unique constraints partial too, or re-inserting a deleted email fails.
- Verify the ORM's default scope actually applies on every path, including raw
  SQL, reports, and joins through the table.
- Consider an archive table instead: move the row, keep the live table small.

## Naming conventions

Pick one and apply it mechanically: `snake_case`; plural table names; `id` for
the surrogate key; `<referenced_table_singular>_id` for FKs; `created_at` /
`updated_at` on every mutable table; index names that carry their purpose
(`idx_<table>_<cols>`, `uq_<table>_<cols>`, `fk_<table>_<ref>`). Consistency is
worth more than the specific choice — it is what makes a missing index obvious.

## Antipattern checklist

Run `scripts/schema_audit.py` to catch these mechanically:

- Table with no primary key (InnoDB builds a hidden one you cannot use;
  PostgreSQL loses replica identity for logical replication).
- Random UUIDv4 primary key, or a UUID stored as 36-byte text.
- Foreign key with no index on the child column (error on PostgreSQL;
  on MySQL a warning — InnoDB auto-creates a narrow one).
- Child/parent type mismatch across a foreign key.
- Money in a binary float type.
- `timestamp without time zone` for an instant.
- An index that is a leftmost prefix of another index on the same table.
- EAV-shaped tables.
- Every column nullable.
- JSON column with no supporting index.
