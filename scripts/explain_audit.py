#!/usr/bin/env python3
"""Analyze a PostgreSQL JSON execution plan and rank the real bottlenecks.

Input: the output of
    EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <query>;
saved to a file, or piped on stdin. MySQL `EXPLAIN FORMAT=JSON` is detected and
reported with the subset of checks that apply (MySQL reports no per-node
buffers and only reports timings with EXPLAIN ANALYZE).

Usage:
    python3 explain_audit.py plan.json [--format text|json] [--top N]

What it computes that reading the plan by eye gets wrong:
  * self time per node (total time x loops, minus children) — the loop
    multiplier is the single most misread number in a nested plan;
  * estimate vs actual row ratio per node, which is what makes the planner
    pick a Nested Loop over a Hash Join;
  * disk spills, lossy bitmaps, heap fetches and buffer read ratios.

Exit codes: 0 = no error-level findings, 1 = at least one, 2 = unparseable input.
"""
from __future__ import annotations

import argparse
import json
import sys

# Thresholds. Each is a documented judgement call, not a magic constant.
MISESTIMATE_RATIO = 10.0      # planner picks wrong join algorithms past ~10x
MISESTIMATE_MIN_ROWS = 100.0  # below this, ratios are noise
SLOW_NODE_SHARE = 0.20        # a node owning >20% of runtime is worth naming
HIGH_LOOPS = 10_000           # loop counts where per-row cost dominates
FILTER_WASTE_RATIO = 10.0     # rows discarded per row returned


def child_nodes(node: dict) -> list[dict]:
    kids = list(node.get("Plans", []))
    return kids


def walk(node: dict, depth: int = 0, parent: dict | None = None):
    yield node, depth, parent
    for kid in child_nodes(node):
        yield from walk(kid, depth + 1, node)


def total_time(node: dict) -> float:
    """Inclusive wall time for every execution of this node, in ms."""
    return float(node.get("Actual Total Time", 0.0)) * float(node.get("Actual Loops", 1) or 1)


def self_time(node: dict) -> float:
    return max(0.0, total_time(node) - sum(total_time(k) for k in child_nodes(node)))


def node_label(node: dict) -> str:
    label = node.get("Node Type", "?")
    for key in ("Relation Name", "Index Name", "CTE Name", "Subplan Name"):
        if node.get(key):
            label += f" on {node[key]}"
            break
    return label


def analyze_postgres(plan_root: dict, execution_time: float | None) -> tuple[list[dict], list[dict]]:
    findings: list[dict] = []
    nodes: list[dict] = []

    root_time = total_time(plan_root) or (execution_time or 0.0)

    for node, depth, _parent in walk(plan_root):
        loops = float(node.get("Actual Loops", 1) or 1)
        act = float(node.get("Actual Rows", 0)) * loops
        est = float(node.get("Plan Rows", 0)) * loops
        st = self_time(node)
        label = node_label(node)
        nodes.append({
            "node": label,
            "depth": depth,
            "self_time_ms": round(st, 3),
            "total_time_ms": round(total_time(node), 3),
            "loops": int(loops),
            "actual_rows": int(act),
            "estimated_rows": int(est),
        })

        def add(fid, sev, msg, fix):
            findings.append({"id": fid, "severity": sev, "node": label,
                             "self_time_ms": round(st, 3), "message": msg, "fix": fix})

        ntype = node.get("Node Type", "")

        if root_time and st / root_time > SLOW_NODE_SHARE and st > 1.0:
            add("PLAN001", "info",
                f"Owns {st / root_time:.0%} of total runtime ({st:.1f} ms of {root_time:.1f} ms).",
                "This is the node to fix; changes elsewhere cannot beat Amdahl's law.")

        if est >= 1 and act >= MISESTIMATE_MIN_ROWS:
            ratio = max(act / est, est / max(act, 1))
            if ratio >= MISESTIMATE_RATIO:
                add("PLAN002", "error",
                    f"Row estimate off by {ratio:.0f}x (estimated {int(est)}, actual {int(act)}).",
                    "The planner chose this shape from the wrong cardinality. Run ANALYZE on "
                    "the table; if it persists, raise the statistics target or add extended "
                    "statistics (CREATE STATISTICS) for correlated columns.")

        if ntype == "Seq Scan" and act > 50_000:
            add("PLAN003", "error",
                f"Sequential scan returning {int(act)} rows from {node.get('Relation Name', '?')}.",
                "Missing index, a non-sargable predicate, or a filter too unselective to "
                "help. Check the Filter line before adding an index.")

        removed = float(node.get("Rows Removed by Filter", 0)) * loops
        if removed > 10_000 and removed > act * FILTER_WASTE_RATIO:
            add("PLAN004", "warn",
                f"Discarded {int(removed)} rows by filter to return {int(act)}.",
                "The access path is far broader than the predicate. Move the filtered column "
                "into the index key (equality columns first), or make it a partial index.")

        if node.get("Sort Method", "").startswith("external") or node.get("Sort Space Type") == "Disk":
            add("PLAN005", "error",
                f"Sort spilled to disk ({node.get('Sort Method')}, "
                f"{node.get('Sort Space Used', '?')} kB).",
                "Raise work_mem for this statement (SET LOCAL work_mem = '64MB' inside the "
                "transaction — it is per operation, not per connection), or supply the order "
                "from an index so the sort disappears.")

        if int(node.get("Hash Batches", 1) or 1) > 1:
            add("PLAN006", "error",
                f"Hash join spilled into {node.get('Hash Batches')} batches "
                f"(peak {node.get('Peak Memory Usage', '?')} kB).",
                "The build side exceeded work_mem. Raise work_mem for the statement or "
                "reduce the build side with an earlier filter.")

        heap_fetches = float(node.get("Heap Fetches", 0))
        if ntype == "Index Only Scan" and heap_fetches > 0:
            sev = "warn" if heap_fetches > max(act * 0.1, 100) else "info"
            add("PLAN007", sev,
                f"Index Only Scan performed {int(heap_fetches)} heap fetches.",
                "The visibility map is stale, so the scan is not index-only in practice. "
                "Run VACUUM (ANALYZE) on the table and tune autovacuum for its write rate.")

        lossy = float(node.get("Lossy Heap Blocks", 0))
        if lossy > 0:
            add("PLAN008", "warn",
                f"Bitmap degraded to lossy ({int(lossy)} lossy blocks).",
                "The bitmap outgrew work_mem, so every row on those pages is rechecked. "
                "Raise work_mem or make the index more selective.")

        inner_loops = max((float(k.get("Actual Loops", 1) or 1) for k in child_nodes(node)), default=0)
        if ntype == "Nested Loop" and inner_loops >= HIGH_LOOPS:
            add("PLAN009", "warn",
                f"Nested Loop drives its inner side {int(inner_loops)} times.",
                "Usually a cardinality misestimate on the outer side. Fix statistics first; "
                "a Hash Join is typically correct at this volume.")

        # Buffer counters are cumulative over children — subtract them so the
        # finding lands on the node that actually did the I/O.
        read = float(node.get("Shared Read Blocks", 0)) - sum(
            float(k.get("Shared Read Blocks", 0)) for k in child_nodes(node))
        hit = float(node.get("Shared Hit Blocks", 0)) - sum(
            float(k.get("Shared Hit Blocks", 0)) for k in child_nodes(node))
        if read > 10_000 and read > hit:
            add("PLAN010", "warn",
                f"Read {int(read)} blocks from disk vs {int(hit)} cache hits.",
                "The working set does not fit the buffer cache for this path. A covering or "
                "partial index shrinks what must be resident.")

    return findings, nodes


def analyze_mysql(block: dict) -> tuple[list[dict], list[dict]]:
    findings: list[dict] = []
    nodes: list[dict] = []

    def visit(obj, path="query_block"):
        if isinstance(obj, dict):
            ti = obj.get("table")
            if isinstance(ti, dict):
                name = ti.get("table_name", "?")
                access = ti.get("access_type", "?")
                rows = ti.get("rows_examined_per_scan") or ti.get("rows") or 0
                nodes.append({"node": f"{access} on {name}", "actual_rows": rows,
                              "key": ti.get("key"), "depth": 0,
                              "self_time_ms": None, "total_time_ms": None, "loops": None,
                              "estimated_rows": rows})
                if access == "ALL" and float(rows or 0) > 50_000:
                    findings.append({"id": "MY001", "severity": "error", "node": name,
                                     "self_time_ms": None,
                                     "message": f"Full table scan (access_type ALL) over ~{rows} rows.",
                                     "fix": "No usable index. Check possible_keys and the "
                                            "leftmost-prefix rule for the WHERE predicate."})
                if ti.get("using_filesort"):
                    findings.append({"id": "MY002", "severity": "warn", "node": name,
                                     "self_time_ms": None,
                                     "message": "Using filesort.",
                                     "fix": "Order the composite index so the ORDER BY columns "
                                            "follow the equality columns, in the same direction."})
                if ti.get("using_temporary_table"):
                    findings.append({"id": "MY003", "severity": "warn", "node": name,
                                     "self_time_ms": None,
                                     "message": "Using temporary table.",
                                     "fix": "GROUP BY/DISTINCT could not stream from an index. "
                                            "Consider an index covering the grouping columns."})
            for v in obj.values():
                visit(v, path)
        elif isinstance(obj, list):
            for v in obj:
                visit(v, path)

    visit(block)
    return findings, nodes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="JSON plan file; omit to read stdin")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--top", type=int, default=8, help="how many nodes to list by self time")
    args = ap.parse_args(argv)

    try:
        raw = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: expected JSON. Re-run the plan with FORMAT JSON "
              f"(PostgreSQL: EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ...). Detail: {exc}",
              file=sys.stderr)
        return 2

    engine = "postgres"
    if isinstance(data, list) and data and "Plan" in data[0]:
        root = data[0]
        findings, nodes = analyze_postgres(root["Plan"], root.get("Execution Time"))
        exec_time = root.get("Execution Time")
        planning = root.get("Planning Time")
    elif isinstance(data, dict) and "Plan" in data:
        findings, nodes = analyze_postgres(data["Plan"], data.get("Execution Time"))
        exec_time = data.get("Execution Time")
        planning = data.get("Planning Time")
    elif isinstance(data, dict) and "query_block" in data:
        engine = "mysql"
        findings, nodes = analyze_mysql(data["query_block"])
        exec_time = planning = None
    else:
        print("ERROR: unrecognized plan JSON (no 'Plan' or 'query_block' key).", file=sys.stderr)
        return 2

    if engine == "postgres" and not any(n.get("total_time_ms") for n in nodes):
        print("WARNING: plan has no actual timings — you ran EXPLAIN without ANALYZE. "
              "Estimates alone cannot show the bottleneck.", file=sys.stderr)

    nodes_sorted = sorted(nodes, key=lambda n: -(n["self_time_ms"] or 0))[: args.top]
    sev_rank = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (sev_rank[f["severity"]], -(f["self_time_ms"] or 0)))

    if args.format == "json":
        print(json.dumps({"engine": engine, "execution_time_ms": exec_time,
                          "planning_time_ms": planning, "hot_nodes": nodes_sorted,
                          "findings": findings}, indent=2))
    else:
        if exec_time is not None:
            print(f"Execution time: {exec_time} ms   Planning time: {planning} ms")
        print(f"\nHottest nodes by self time (total time x loops, children subtracted):")
        for n in nodes_sorted:
            st = f"{n['self_time_ms']:>9.2f} ms" if n["self_time_ms"] is not None else "        -"
            print(f"  {st}  rows={n['actual_rows']:<10} est={n['estimated_rows']:<10} "
                  f"loops={n['loops']}  {n['node']}")
        print(f"\n{len(findings)} finding(s):\n")
        for f in findings:
            print(f"[{f['severity'].upper():5}] {f['id']}  {f['node']}")
            print(f"        {f['message']}")
            print(f"        FIX: {f['fix']}\n")

    return 1 if any(f["severity"] == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
