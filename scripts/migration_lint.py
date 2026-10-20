#!/usr/bin/env python3
"""Lint migration files for operations that lock, rewrite, or break a running deploy.

Works on raw SQL (Flyway, Liquibase formatted SQL, golang-migrate, Atlas) and on
framework migrations (Alembic, Django, Laravel, Doctrine, TypeORM, Knex, Drizzle,
Rails) by matching both the SQL and the framework DSL.

Usage:
    python3 migration_lint.py <file|dir> [...] [--engine postgres|mysql]
                              [--format text|json] [--min-severity info|warn|error]

Exit codes: 0 = no ERROR findings, 1 = at least one ERROR, 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

SEVERITY_ORDER = {"info": 0, "warn": 1, "error": 2}
SUFFIXES = (".sql", ".py", ".php", ".ts", ".js", ".rb", ".go", ".xml", ".yaml", ".yml", ".json")

VOLATILE_DEFAULTS = r"(now\(\)|current_timestamp|clock_timestamp\(\)|random\(\)|gen_random_uuid\(\)|uuid_generate_v4\(\)|uuid\(\))"


class Rule:
    def __init__(self, rid, severity, pattern, message, fix, engines=("postgres", "mysql"),
                 unless=None, flags=re.I):
        self.id = rid
        self.severity = severity
        self.pattern = re.compile(pattern, flags)
        self.message = message
        self.fix = fix
        self.engines = engines
        self.unless = re.compile(unless, re.I) if unless else None


# --- rules that read one logical line/statement -----------------------------
LINE_RULES = [
    Rule("MIG001", "error",
         r"\bCREATE\s+(UNIQUE\s+)?INDEX\b",
         "CREATE INDEX without CONCURRENTLY locks out writes for the whole build.",
         "PostgreSQL: CREATE INDEX CONCURRENTLY (outside any transaction). "
         "MySQL 8: add ALGORITHM=INPLACE, LOCK=NONE.",
         engines=("postgres",), unless=r"CONCURRENTLY"),

    Rule("MIG002", "error",
         r"\bCREATE\s+(UNIQUE\s+)?INDEX\b(?![^;]*ALGORITHM\s*=)",
         "CREATE INDEX without an explicit ALGORITHM/LOCK clause.",
         "State the intent so the server errors instead of silently copying the table: "
         "ALTER TABLE t ADD INDEX i (c), ALGORITHM=INPLACE, LOCK=NONE;",
         engines=("mysql",)),

    Rule("MIG003", "error",
         r"\bALTER\s+TABLE\b[^;]*\bALTER\s+COLUMN\b[^;]*\b(TYPE|SET\s+DATA\s+TYPE)\b",
         "ALTER COLUMN ... TYPE rewrites the whole table and every index under "
         "ACCESS EXCLUSIVE.",
         "Expand/contract instead: add the new column, backfill in batches, dual-write, "
         "swap readers, drop the old column in a later release.",
         engines=("postgres",)),

    Rule("MIG004", "error",
         r"\bALTER\s+TABLE\b[^;]*\b(CHANGE|MODIFY)\s+(COLUMN\s+)?\w+[^;]*\b(BIGINT|INT|VARCHAR|TEXT|DECIMAL|DATETIME|TIMESTAMP)\b",
         "Changing a column's data type in InnoDB is ALGORITHM=COPY: full rebuild, "
         "no concurrent DML.",
         "Use gh-ost/pt-online-schema-change, or expand/contract with a new column. "
         "Exception: widening VARCHAR within the same length-byte class (<=255 or >=256) "
         "is INPLACE.",
         engines=("mysql",)),

    Rule("MIG005", "warn",
         r"\bALTER\s+TABLE\b[^;]*\bSET\s+NOT\s+NULL\b",
         "SET NOT NULL scans the whole table while holding ACCESS EXCLUSIVE.",
         "Two steps: ALTER TABLE t ADD CONSTRAINT c CHECK (col IS NOT NULL) NOT VALID; "
         "ALTER TABLE t VALIDATE CONSTRAINT c;  then SET NOT NULL skips the scan (PG 12+).",
         engines=("postgres",)),

    Rule("MIG006", "warn",
         r"\bADD\s+(CONSTRAINT\s+\S+\s+)?(FOREIGN\s+KEY|CHECK)\b",
         "Adding a validated constraint scans the table and blocks writes.",
         "Add it NOT VALID first, then VALIDATE CONSTRAINT in a second statement "
         "(SHARE UPDATE EXCLUSIVE, concurrent writes allowed).",
         engines=("postgres",), unless=r"NOT\s+VALID"),

    Rule("MIG007", "warn",
         r"\bADD\s+(CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\b",
         "Adding a foreign key in InnoDB falls back to ALGORITHM=COPY unless "
         "foreign_key_checks=0.",
         "Create the child index first, then add the FK with foreign_key_checks=0 in a "
         "controlled window, or use an online schema change tool.",
         engines=("mysql",)),

    Rule("MIG008", "error",
         r"\bADD\s+COLUMN\b[^;]*\bDEFAULT\s+" + VOLATILE_DEFAULTS,
         "ADD COLUMN with a volatile DEFAULT rewrites every row.",
         "A constant default is metadata-only (PG 11+, MySQL 8 INSTANT). For now()/uuid, "
         "add the column nullable, backfill in batches, then set the default.",
         engines=("postgres", "mysql")),

    Rule("MIG009", "warn",
         r"\b(DROP\s+COLUMN|DROP\s+TABLE|RENAME\s+(COLUMN\s+)?\w+\s+TO|RENAME\s+TO)\b",
         "Destructive or renaming DDL breaks any instance of the old code still running.",
         "Expand/contract: ship the additive change, deploy code that stops using the old "
         "object, drop it in a later release. Never rename in place.",
         engines=("postgres", "mysql")),

    Rule("MIG010", "warn",
         r"^\s*(UPDATE|DELETE)\s+(?!.*\bWHERE\b)[^;]*;",
         "Unbounded UPDATE/DELETE: one transaction touching every row.",
         "Batch it by primary key (… WHERE id BETWEEN :lo AND :hi, committing each chunk). "
         "PostgreSQL: one huge UPDATE doubles the heap and stalls autovacuum; InnoDB: the "
         "undo log grows until commit.",
         flags=re.I | re.M),

    Rule("MIG011", "warn",
         r"\bVACUUM\s+FULL\b|\bREINDEX\s+(?!.*CONCURRENTLY)(TABLE|INDEX|DATABASE)\b|\bOPTIMIZE\s+TABLE\b",
         "Blocking maintenance command inside a migration.",
         "VACUUM FULL and plain REINDEX take ACCESS EXCLUSIVE; OPTIMIZE TABLE rebuilds the "
         "InnoDB table. Use REINDEX INDEX CONCURRENTLY, or run it in a maintenance window — "
         "not in a deploy step.",
         flags=re.I),

]

# --- rules that read the whole file (framework context) ---------------------
FILE_RULES = [
    ("MIG020", "error",
     lambda t, p: p.endswith(".py") and "AddIndexConcurrently" in t and not re.search(r"atomic\s*=\s*False", t),
     "Django AddIndexConcurrently/RemoveIndexConcurrently without `atomic = False`.",
     "CONCURRENTLY cannot run inside a transaction. Add `atomic = False` to the Migration "
     "class or the migration fails at runtime."),

    ("MIG021", "error",
     lambda t, p: p.endswith(".py") and re.search(r"postgresql_concurrently\s*=\s*True", t)
     and "autocommit_block" not in t,
     "Alembic concurrent index build inside the default transactional migration.",
     "Wrap it: `with op.get_context().autocommit_block(): op.create_index(..., "
     "postgresql_concurrently=True)`."),

    ("MIG022", "error",
     lambda t, p: p.endswith(".rb") and re.search(r"\balgorithm:\s*:concurrently", t)
     and "disable_ddl_transaction!" not in t,
     "Rails concurrent index without `disable_ddl_transaction!`.",
     "Add `disable_ddl_transaction!` to the migration class."),

    ("MIG023", "warn",
     lambda t, p: p.endswith(".py") and "RunPython" in t and not re.search(r"reverse_code|noop", t),
     "Django RunPython data migration with no reverse_code.",
     "Supply reverse_code (or migrations.RunPython.noop) so the migration is reversible and "
     "`migrate <app> <previous>` does not dead-end."),

    ("MIG024", "warn",
     lambda t, p: p.endswith(".php") and re.search(r"Schema::(table|create)", t)
     and re.search(r"DB::(table|statement|update)\(", t),
     "Laravel migration mixes schema changes and data backfill in one file.",
     "MySQL commits DDL implicitly, so a later failure cannot roll back the DDL and leaves "
     "a half-applied migration. Split schema and backfill into separate migrations."),

    ("MIG025", "warn",
     lambda t, p: p.endswith(".ts") and re.search(r"\bsynchronize\s*:\s*true", t),
     "TypeORM `synchronize: true`.",
     "It silently drops and recreates columns to match entities. Never enable it outside a "
     "throwaway local database; generate migrations instead."),

    ("MIG026", "warn",
     lambda t, p: p.endswith(".sql") and re.search(r"CREATE\s+INDEX\s+CONCURRENTLY", t, re.I)
     and re.search(r"^\s*BEGIN\s*;", t, re.I | re.M),
     "CREATE INDEX CONCURRENTLY inside an explicit BEGIN block.",
     "PostgreSQL rejects it with 'CREATE INDEX CONCURRENTLY cannot run inside a transaction "
     "block'. Remove the BEGIN/COMMIT, and tell the runner not to wrap this migration "
     "(Flyway: executeInTransaction=false; golang-migrate: -x / no-tx directive)."),

    ("MIG027", "warn",
     lambda t, p: p.endswith(".sql") and not re.search(r"lock_timeout|lock_wait_timeout", t, re.I)
     and re.search(r"ALTER\s+TABLE|CREATE\s+INDEX", t, re.I),
     "No lock_timeout guard in a migration containing ALTER TABLE / CREATE INDEX.",
     "DDL queues behind any long-running query and then blocks every query behind it "
     "(PostgreSQL lock queue / InnoDB metadata lock). Open with `SET lock_timeout = '3s';` "
     "(PostgreSQL) or `SET SESSION lock_wait_timeout = 3;` (MySQL) so a blocked statement "
     "fails fast and is retried instead of freezing the table."),
]


def scan_file(path: str, engine: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"WARNING: skipping {path}: {exc}", file=sys.stderr)
        return []

    findings = []
    # Strip -- and # comments so commented-out DDL does not fire rules.
    scrub = re.sub(r"--[^\n]*", "", text)
    scrub = re.sub(r"/\*.*?\*/", "", scrub, flags=re.S)

    for rule in LINE_RULES:
        if engine not in rule.engines:
            continue
        for m in rule.pattern.finditer(scrub):
            window = scrub[m.start(): m.start() + 400]
            if rule.unless and rule.unless.search(window):
                continue
            findings.append({
                "id": rule.id, "severity": rule.severity, "file": path,
                "line": scrub.count("\n", 0, m.start()) + 1,
                "snippet": " ".join(m.group(0).split())[:120],
                "message": rule.message, "fix": rule.fix,
            })

    for rid, sev, test, msg, fix in FILE_RULES:
        try:
            hit = test(text, path)
        except Exception:  # a malformed file must not kill the lint run
            hit = False
        if hit:
            findings.append({"id": rid, "severity": sev, "file": path, "line": 1,
                             "snippet": "", "message": msg, "fix": fix})
    return findings


def collect(paths: list[str]) -> list[str]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                if any(part in root for part in (os.sep + "node_modules", os.sep + ".git", os.sep + "vendor")):
                    continue
                files.extend(os.path.join(root, n) for n in sorted(names) if n.endswith(SUFFIXES))
        else:
            files.append(p)
    return files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="migration files or directories")
    ap.add_argument("--engine", choices=["postgres", "mysql"], default="postgres")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--min-severity", choices=["info", "warn", "error"], default="info")
    args = ap.parse_args(argv)

    files = collect(args.paths)
    if not files:
        print("ERROR: no migration files found with a known suffix "
              f"({', '.join(SUFFIXES)}).", file=sys.stderr)
        return 2

    findings = []
    for path in files:
        findings.extend(scan_file(path, args.engine))
    findings = [f for f in findings if SEVERITY_ORDER[f["severity"]] >= SEVERITY_ORDER[args.min_severity]]
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]] * -1, f["file"], f["line"]))

    if args.format == "json":
        print(json.dumps({"engine": args.engine, "files_scanned": len(files),
                          "findings": findings}, indent=2))
    else:
        print(f"Scanned {len(files)} file(s); {len(findings)} finding(s).\n")
        for f in findings:
            print(f"[{f['severity'].upper():5}] {f['id']}  {f['file']}:{f['line']}")
            if f["snippet"]:
                print(f"        > {f['snippet']}")
            print(f"        {f['message']}")
            print(f"        FIX: {f['fix']}\n")

    return 1 if any(f["severity"] == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
