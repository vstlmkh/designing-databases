# Contributing

Thanks for taking the time. This repository is a single Agent Skill, so the bar
is narrower than a normal library: everything here is read by a model under a
token budget, and every claim it makes ends up in someone's production database.

## Ground rules

1. **Claims must be verifiable.** Every engine behaviour stated in `SKILL.md` or
   `references/` must be true of a supported release (PostgreSQL 13+, MySQL 8.0+)
   and checkable against the official documentation. Name the version whenever a
   behaviour changed across one.
2. **Say which engine.** PostgreSQL and InnoDB make opposite choices about
   clustering, MVCC and locking. A sentence that does not say which engine it
   applies to is a bug.
3. **No destructive defaults.** Nothing in this skill may suggest applying DDL,
   DML or `EXPLAIN ANALYZE` on a writing statement without explicit approval.
4. **Budget the tokens.** `SKILL.md` is loaded on every activation; keep it a
   router and a checklist. Depth belongs in `references/`, which is loaded on
   demand. Do not duplicate a reference's content in `SKILL.md`.

## Where a change belongs

| Change | File |
|---|---|
| Routing, a workflow step, a gotcha | `SKILL.md` |
| Depth on one topic | the matching `references/*.md` |
| A new automated check | `scripts/` + a note in `SKILL.md` → Scripts |
| Reusable SQL or an output format | `assets/` |
| Proof that a change works | `evals/` |

## Scripts

- Python 3.9+, **standard library only**. No third-party packages, no network,
  no filesystem writes outside the paths given on the command line.
- Keep the shared contract: findings to stdout, diagnostics to stderr, exit `0`
  clean / `1` on an ERROR finding / `2` on usage or parse error, and both
  `--format text` and `--format json`.
- Parsing stays tolerant. `sqlparse_lite.py` is deliberately not a real parser;
  a new check should degrade quietly on SQL it cannot read rather than crash.
- A new check needs a false-positive story: say what it will flag that is
  actually fine, and why the signal is still worth it.

Run them before sending a change:

```bash
python3 scripts/schema_audit.py evals/files/legacy_schema.sql --engine postgres
python3 scripts/explain_audit.py evals/files/slow_plan.json
python3 scripts/migration_lint.py evals/files/migrations --engine postgres
```

## Evals

Behaviour changes come with an eval. Each file in `evals/` is JSON:

- `query` — what the user says, in the words they would really use;
- `files` — any fixtures under `evals/files/` the query refers to;
- `should_trigger` — `true`, or `false` for a negative case;
- `expected_behavior` — observable statements about the answer, not phrasing.

Negative cases matter as much as positive ones: a near-miss and an
out-of-scope case (NoSQL) keep the skill from firing on everything.

## Pull requests

- One topic per PR; keep the diff readable.
- Describe the engine behaviour you are asserting and link the documentation.
- Note whether `SKILL.md` grew, and if so what you removed to pay for it.
- Bump `metadata.version` in the `SKILL.md` frontmatter for anything that
  changes behaviour (semver: patch for a correction, minor for new coverage,
  major for a restructure of the routing or workflows).

By contributing you agree that your work is licensed under the MIT License in
[LICENSE.md](LICENSE.md).
