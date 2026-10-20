#!/usr/bin/env python3
"""Audit a SQL schema (DDL) for structural defects that cost performance or integrity.

Input: one or more files containing CREATE TABLE / CREATE INDEX / ALTER TABLE
statements — a `pg_dump --schema-only`, a `mysqldump --no-data`, or plain
migration SQL. Reads stdin when no file is given.

Usage:
    python3 schema_audit.py schema.sql [more.sql ...] [--engine postgres|mysql]
                            [--format text|json] [--min-severity info|warn|error]

Exit codes: 0 = no ERROR findings, 1 = at least one ERROR, 2 = usage/parse error.
Findings go to stdout; diagnostics to stderr.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict

from sqlparse_lite import paren_body, split_columns, split_statements, unquote

SEVERITY_ORDER = {"info": 0, "warn": 1, "error": 2}

# Column-name heuristics. Kept explicit so a reader can see exactly what fires.
MONEY_WORDS = ("price", "amount", "total", "balance", "cost", "fee", "salary", "sum")
APPROX_TYPES = ("float", "double", "real")
SOFT_DELETE_COLS = ("deleted_at", "is_deleted", "deleted", "archived_at")
EAV_VALUE_COLS = ("value", "attribute_value", "val", "attr_value")
EAV_KEY_COLS = ("attribute", "attribute_name", "attr", "attr_name", "key", "property")


@dataclass
class Column:
    name: str
    type: str
    raw: str
    not_null: bool
    line: int


@dataclass
class Table:
    name: str
    line: int
    columns: dict[str, Column] = field(default_factory=dict)
    pk: list[str] = field(default_factory=list)
    fks: list[dict] = field(default_factory=list)
    indexes: list[dict] = field(default_factory=list)
    uniques: list[list[str]] = field(default_factory=list)


@dataclass
class Finding:
    id: str
    severity: str
    table: str
    object: str
    message: str
    fix: str
    line: int


# --------------------------------------------------------------------------- parse

CREATE_TABLE_RE = re.compile(
    r"^\s*CREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)",
    re.I,
)
CREATE_INDEX_RE = re.compile(
    r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([^\s(]+)?\s*ON\s+(?:ONLY\s+)?([^\s(]+)",
    re.I,
)
ALTER_TABLE_RE = re.compile(r"^\s*ALTER\s+TABLE\s+(?:ONLY\s+)?([^\s]+)", re.I)
FK_RE = re.compile(
    r"FOREIGN\s+KEY\s*\(([^)]*)\)\s*REFERENCES\s+([^\s(]+)\s*\(([^)]*)\)", re.I
)
INLINE_REF_RE = re.compile(r"\bREFERENCES\s+([^\s(]+)\s*\(([^)]*)\)", re.I)
PK_INLINE_RE = re.compile(r"\bPRIMARY\s+KEY\b", re.I)
PK_TABLE_RE = re.compile(r"^\s*(?:CONSTRAINT\s+\S+\s+)?PRIMARY\s+KEY\s*\(([^)]*)\)", re.I)
UNIQUE_TABLE_RE = re.compile(r"^\s*(?:CONSTRAINT\s+\S+\s+)?UNIQUE\s*\(([^)]*)\)", re.I)
# A type expression: base word, optional modifier words (double precision,
# character varying, int unsigned), optional (n[,m]), optional time-zone tail,
# optional array suffix. Anything after that is constraints/defaults.
TYPE_RE = re.compile(
    r"^\s*([A-Za-z_][\w\.]*"
    r"(?:\s+(?:precision|varying|unsigned|signed|zerofill))*"
    r"(?:\s*\([^)]*\))?"
    r"(?:\s+(?:with|without)\s+time\s+zone)?"
    r"(?:\s*\[\s*\])*)",
    re.I,
)
CONSTRAINT_KEYWORDS = ("primary", "unique", "foreign", "constraint", "check", "key", "index", "exclude")


def norm_name(raw: str) -> str:
    """`public.users` / "Users" / `users` -> users (lowercased, schema dropped)."""
    parts = [unquote(p) for p in raw.strip().rstrip(",").split(".")]
    return parts[-1].lower()


def col_list(raw: str) -> list[str]:
    cols = []
    for part in split_columns(raw):
        part = part.strip()
        part = re.sub(r"\s+(ASC|DESC|NULLS\s+FIRST|NULLS\s+LAST)$", "", part, flags=re.I)
        part = re.sub(r"\s+\S+_ops$", "", part)  # PostgreSQL opclass
        part = re.sub(r"\(\d+\)$", "", part)  # MySQL prefix length
        if part:
            cols.append(unquote(part).lower())
    return cols


def parse(sql: str, line_offset: int, tables: dict[str, Table]) -> None:
    for stmt in split_statements(sql):
        line = stmt.line + line_offset
        head = stmt.text.lstrip()
        m = CREATE_TABLE_RE.match(head)
        if m:
            name = norm_name(m.group(1))
            body, _ = paren_body(stmt.text, m.end(1))
            table = tables.setdefault(name, Table(name=name, line=line))
            table.line = line
            for item in split_columns(body):
                low = item.lstrip().lower()
                first = low.split()[0] if low.split() else ""
                if first in CONSTRAINT_KEYWORDS:
                    pk = PK_TABLE_RE.match(item)
                    if pk:
                        table.pk = col_list(pk.group(1))
                    uq = UNIQUE_TABLE_RE.match(item)
                    if uq:
                        table.uniques.append(col_list(uq.group(1)))
                    fk = FK_RE.search(item)
                    if fk:
                        table.fks.append(
                            {
                                "columns": col_list(fk.group(1)),
                                "ref_table": norm_name(fk.group(2)),
                                "ref_columns": col_list(fk.group(3)),
                                "line": line,
                            }
                        )
                    continue
                parts = item.split(None, 1)
                if len(parts) < 2:
                    continue
                cname = unquote(parts[0]).lower()
                tmatch = TYPE_RE.match(parts[1])
                ctype = (tmatch.group(1) if tmatch else parts[1]).strip().lower()
                column = Column(
                    name=cname,
                    type=ctype,
                    raw=item,
                    not_null=bool(re.search(r"\bNOT\s+NULL\b", item, re.I)),
                    line=line,
                )
                table.columns[cname] = column
                if PK_INLINE_RE.search(item):
                    table.pk = [cname]
                if re.search(r"\bUNIQUE\b", item, re.I):
                    table.uniques.append([cname])
                ref = INLINE_REF_RE.search(item)
                if ref:
                    table.fks.append(
                        {
                            "columns": [cname],
                            "ref_table": norm_name(ref.group(1)),
                            "ref_columns": col_list(ref.group(2)),
                            "line": line,
                        }
                    )
            continue

        m = CREATE_INDEX_RE.match(head)
        if m:
            tname = norm_name(m.group(3))
            body, _ = paren_body(stmt.text, m.end(3))
            table = tables.setdefault(tname, Table(name=tname, line=line))
            cols = col_list(body)
            where = re.search(r"\)\s*(?:INCLUDE\s*\([^)]*\)\s*)?WHERE\s+(.+)$", stmt.text, re.I | re.S)
            idx = {
                "name": norm_name(m.group(2) or "(unnamed)"),
                "columns": cols,
                "unique": bool(m.group(1)),
                "partial": bool(where),
                "method": (re.search(r"USING\s+(\w+)", stmt.text, re.I).group(1).lower()
                           if re.search(r"USING\s+(\w+)", stmt.text, re.I) else "btree"),
                "line": line,
            }
            table.indexes.append(idx)
            if idx["unique"]:
                table.uniques.append(cols)
            continue

        m = ALTER_TABLE_RE.match(head)
        if m:
            tname = norm_name(m.group(1))
            table = tables.setdefault(tname, Table(name=tname, line=line))
            for fk in FK_RE.finditer(stmt.text):
                table.fks.append(
                    {
                        "columns": col_list(fk.group(1)),
                        "ref_table": norm_name(fk.group(2)),
                        "ref_columns": col_list(fk.group(3)),
                        "line": line,
                    }
                )
            pk = re.search(r"ADD\s+(?:CONSTRAINT\s+\S+\s+)?PRIMARY\s+KEY\s*\(([^)]*)\)", stmt.text, re.I)
            if pk:
                table.pk = col_list(pk.group(1))
            uq = re.search(r"ADD\s+(?:CONSTRAINT\s+\S+\s+)?UNIQUE\s*\(([^)]*)\)", stmt.text, re.I)
            if uq:
                table.uniques.append(col_list(uq.group(1)))
            for add_col in re.finditer(r"ADD\s+COLUMN\s+(\S+)\s+([^,;]+)", stmt.text, re.I):
                cname = unquote(add_col.group(1)).lower()
                tmatch = TYPE_RE.match(add_col.group(2))
                table.columns.setdefault(
                    cname,
                    Column(
                        name=cname,
                        type=(tmatch.group(1) if tmatch else add_col.group(2)).strip().lower(),
                        raw=add_col.group(0),
                        not_null=bool(re.search(r"\bNOT\s+NULL\b", add_col.group(2), re.I)),
                        line=line,
                    ),
                )


# --------------------------------------------------------------------------- checks


def index_covers(table: Table, column: str) -> bool:
    """True when some index/unique/PK can drive a lookup on `column` (leftmost)."""
    if table.pk and table.pk[0] == column:
        return True
    for cols in table.uniques:
        if cols and cols[0] == column:
            return True
    for idx in table.indexes:
        if idx["columns"] and idx["columns"][0] == column and not idx["partial"]:
            return True
    return False


def is_random_uuid_pk(col: Column) -> bool:
    t = col.type.replace(" ", "")
    if t.startswith(("char(36)", "varchar(36)", "character(36)", "charactervarying(36)")):
        return True
    if t in ("uuid", "binary(16)"):
        return True
    return False


def audit(tables: dict[str, Table], engine: str) -> list[Finding]:
    out: list[Finding] = []

    def add(fid, sev, table, obj, msg, fix, line):
        out.append(Finding(fid, sev, table, obj, msg, fix, line))

    for name, t in sorted(tables.items()):
        if not t.columns:
            continue  # index-only reference to a table defined elsewhere

        # --- primary keys
        if not t.pk:
            add("PK001", "error", name, "", "Table has no PRIMARY KEY.",
                "InnoDB builds a hidden 6-byte clustered key you cannot use; PostgreSQL "
                "loses replica identity for logical replication. Add an explicit key.", t.line)
        else:
            pk_cols = [t.columns.get(c) for c in t.pk if t.columns.get(c)]
            if len(pk_cols) == 1:
                c = pk_cols[0]
                if is_random_uuid_pk(c) and "default" in c.raw.lower() and re.search(
                    r"uuid_generate_v4|gen_random_uuid|uuid\(\)", c.raw, re.I
                ):
                    add("PK002", "error", name, c.name,
                        f"Primary key `{c.name}` defaults to a random UUIDv4.",
                        "Random keys split B+tree leaf pages on every insert (InnoDB: the "
                        "clustered index IS the table). Use BIGINT identity, or UUIDv7/ULID "
                        "stored as uuid/BINARY(16) if IDs must be non-enumerable.", c.line)
                elif is_random_uuid_pk(c):
                    add("PK003", "warn", name, c.name,
                        f"Primary key `{c.name}` is a UUID-shaped type.",
                        "Confirm the generator is time-ordered (UUIDv7/ULID). UUIDv4 causes "
                        "page splits and inflates every secondary index.", c.line)
                if c.type.replace(" ", "").startswith(("char(36)", "varchar(36)", "charactervarying(36)")):
                    add("PK004", "warn", name, c.name,
                        f"Primary key `{c.name}` is a 36-byte text UUID.",
                        "Every secondary index stores the PK (InnoDB) — 36 bytes vs 16 for "
                        "uuid/BINARY(16). Store UUIDs in a native binary type.", c.line)
                if re.match(r"^(int|integer|int4|serial|mediumint|smallint)\b", c.type):
                    add("PK005", "info", name, c.name,
                        f"Primary key `{c.name}` is 32-bit ({c.type}); ceiling ~2.1e9 rows.",
                        "Widening a PK later rewrites the table and every FK. Use BIGINT now "
                        "unless the row count is provably bounded.", c.line)

        # --- foreign keys
        for fk in t.fks:
            child = fk["columns"][0] if fk["columns"] else ""
            if child and not index_covers(t, child):
                if engine == "mysql":
                    msg_fix = (f"InnoDB will silently create a single-column index named after "
                               f"the constraint — rarely the one the queries need. Declare it "
                               f"explicitly, widened for the real access path: "
                               f"ALTER TABLE {name} ADD INDEX idx_{name}_{child} ({child}), "
                               f"ALGORITHM=INPLACE, LOCK=NONE;")
                else:
                    msg_fix = (f"PostgreSQL does not index FK children. Every parent DELETE or "
                               f"key UPDATE then scans {name} while holding locks. "
                               f"CREATE INDEX CONCURRENTLY idx_{name}_{child} ON {name} ({child});")
                add("FK001", "error" if engine == "postgres" else "warn", name, child,
                    f"Foreign key column `{child}` has no index with it as the leftmost column.",
                    msg_fix, fk["line"])
            parent = tables.get(fk["ref_table"])
            if parent and fk["ref_columns"]:
                pcol = parent.columns.get(fk["ref_columns"][0])
                ccol = t.columns.get(child)
                if pcol and ccol:
                    pbase = re.split(r"[ (]", pcol.type)[0]
                    cbase = re.split(r"[ (]", ccol.type)[0]
                    ints = {"int", "integer", "int4", "serial", "bigint", "int8", "bigserial", "smallint"}
                    if pbase != cbase and not (pbase in ints and cbase in ints and pbase == cbase):
                        if pbase in ints and cbase in ints:
                            add("FK002", "error", name, child,
                                f"FK `{name}.{child}` is {ccol.type} but "
                                f"{parent.name}.{pcol.name} is {pcol.type}.",
                                "Width mismatch overflows on large parents and can force an "
                                "implicit cast that disables index-driven joins. Match the types.",
                                fk["line"])
                        else:
                            add("FK003", "warn", name, child,
                                f"FK `{name}.{child}` ({ccol.type}) and parent "
                                f"{parent.name}.{pcol.name} ({pcol.type}) have different types.",
                                "Mismatched join types prevent index seeks. Align them.", fk["line"])

        # --- indexes
        all_keys = [(i["name"], i["columns"], i["partial"], i["line"]) for i in t.indexes]
        for i, (n1, c1, p1, l1) in enumerate(all_keys):
            for n2, c2, p2, _ in all_keys:
                if n1 == n2 or p1 or p2 or not c1 or not c2:
                    continue
                if c1 == c2 and n1 > n2:
                    add("IDX001", "warn", name, n1,
                        f"Index `{n1}` duplicates `{n2}` ({', '.join(c1)}).",
                        "Drop one. Every write maintains both.", l1)
                elif len(c1) < len(c2) and c2[: len(c1)] == c1:
                    add("IDX002", "warn", name, n1,
                        f"Index `{n1}` ({', '.join(c1)}) is a leftmost prefix of "
                        f"`{n2}` ({', '.join(c2)}).",
                        f"The wider index already serves those queries. Drop `{n1}` after "
                        "confirming zero scans on primary AND replicas.", l1)

        # --- columns
        non_pk = [c for c in t.columns.values() if c.name not in t.pk]
        nullable = [c for c in non_pk if not c.not_null]
        if non_pk and len(nullable) == len(non_pk) and len(non_pk) >= 3:
            add("COL001", "warn", name, "",
                "No column on this table is NOT NULL.",
                "Nullability is a business statement. Mandate NOT NULL unless a requirement "
                "explicitly allows the unknown value; NULLs break = comparisons and aggregates.",
                t.line)

        for c in t.columns.values():
            base = re.split(r"[ (]", c.type)[0]
            if base in APPROX_TYPES and any(w in c.name for w in MONEY_WORDS):
                add("COL002", "error", name, c.name,
                    f"Money-like column `{c.name}` uses approximate type {c.type}.",
                    "Binary floats cannot represent 0.10 exactly; sums drift. Use "
                    "NUMERIC/DECIMAL(p,s), or an integer count of minor units.", c.line)
            if engine == "postgres" and base in ("timestamp", "datetime") and "with time zone" not in c.type:
                add("COL003", "warn", name, c.name,
                    f"`{c.name}` is {c.type} (no time zone).",
                    "timestamp without time zone silently reinterprets values per session "
                    "TimeZone. Use TIMESTAMPTZ.", c.line)
            if engine == "mysql" and base == "datetime":
                add("COL004", "info", name, c.name,
                    f"`{c.name}` is DATETIME.",
                    "DATETIME has no time-zone conversion; TIMESTAMP converts to/from UTC "
                    "with the session zone. Pick deliberately and store UTC.", c.line)
            if base in ("json", "jsonb") and not any(
                c.name in idx["columns"] or idx["method"] in ("gin", "gist") for idx in t.indexes
            ):
                add("COL005", "info", name, c.name,
                    f"`{c.name}` is {base} with no supporting index.",
                    "Filtering inside a document is a full scan. Add a GIN index for "
                    "containment (@>) or an expression index on the hot path.", c.line)
            if base == "enum":
                add("COL006", "info", name, c.name,
                    f"`{c.name}` is an ENUM.",
                    "Adding a value is a schema migration, and MySQL reorders silently on "
                    "mid-list inserts. A lookup table or a CHECK constraint is cheaper to evolve.",
                    c.line)

        # --- antipatterns
        cols_lower = set(t.columns)
        if cols_lower & set(EAV_KEY_COLS) and cols_lower & set(EAV_VALUE_COLS) and len(t.columns) <= 7:
            add("AP001", "warn", name, "",
                "Table shape looks like Entity-Attribute-Value.",
                "EAV cannot carry foreign keys, types or ranges; reading one entity needs N "
                "self-joins. Use real columns, or a JSONB/JSON column with an index on the "
                "queried paths.", t.line)

        for sd in SOFT_DELETE_COLS:
            if sd in t.columns:
                has_partial = any(i["partial"] for i in t.indexes)
                add("AP002", "info", name, sd,
                    f"Soft-delete column `{sd}` present.",
                    ("Every query must filter it, and full indexes carry dead rows. Consider "
                     f"partial indexes (… WHERE {sd} IS NULL) on the hot paths."
                     if not has_partial else
                     f"Confirm all hot-path indexes are partial on {sd} and that the ORM "
                     "default scope actually applies."), t.columns[sd].line)
                break

    return out


# --------------------------------------------------------------------------- output


def render_text(findings: list[Finding], tables: dict[str, Table]) -> str:
    lines = [f"Parsed {len(tables)} table(s); {len(findings)} finding(s).", ""]
    if not findings:
        lines.append("No findings at the requested severity.")
        return "\n".join(lines)
    order = {"error": 0, "warn": 1, "info": 2}
    for f in sorted(findings, key=lambda x: (order[x.severity], x.table, x.id)):
        target = f"{f.table}.{f.object}" if f.object else f.table
        lines.append(f"[{f.severity.upper():5}] {f.id} line {f.line}  {target}")
        lines.append(f"        {f.message}")
        lines.append(f"        FIX: {f.fix}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="SQL DDL files; omit to read stdin")
    ap.add_argument("--engine", choices=["postgres", "mysql"], default="postgres")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--min-severity", choices=["info", "warn", "error"], default="info")
    args = ap.parse_args(argv)

    tables: dict[str, Table] = {}
    try:
        if args.files:
            for path in args.files:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    parse(fh.read(), 0, tables)
        else:
            parse(sys.stdin.read(), 0, tables)
    except OSError as exc:
        print(f"ERROR: cannot read input: {exc}", file=sys.stderr)
        return 2

    if not tables:
        print("ERROR: no CREATE TABLE / CREATE INDEX / ALTER TABLE statements found. "
              "Pass a schema dump (pg_dump --schema-only or mysqldump --no-data).", file=sys.stderr)
        return 2

    findings = [f for f in audit(tables, args.engine)
                if SEVERITY_ORDER[f.severity] >= SEVERITY_ORDER[args.min_severity]]

    if args.format == "json":
        print(json.dumps(
            {"engine": args.engine,
             "tables": sorted(tables),
             "findings": [asdict(f) for f in findings]},
            indent=2))
    else:
        print(render_text(findings, tables))

    return 1 if any(f.severity == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
