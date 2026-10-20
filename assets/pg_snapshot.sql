-- PostgreSQL read-only snapshot pack.
-- Usage: psql "$DATABASE_URL" -X -f assets/pg_snapshot.sql > snapshot.txt
-- Safe on a replica. Every statement is read-only.

\set ON_ERROR_STOP off
\timing off
\pset pager off

SET default_transaction_read_only = on;
SET statement_timeout = '60s';
SET lock_timeout = '2s';

\echo '===== 1. VERSION AND KEY SETTINGS ====='
SELECT version();
SELECT name, setting, unit
FROM pg_settings
WHERE name IN ('shared_buffers','work_mem','maintenance_work_mem','effective_cache_size',
               'random_page_cost','seq_page_cost','max_connections','max_parallel_workers_per_gather',
               'default_statistics_target','autovacuum','autovacuum_vacuum_scale_factor',
               'autovacuum_analyze_scale_factor','synchronous_commit','wal_level')
ORDER BY name;

\echo '===== 2. DATABASE AND TABLE SIZES ====='
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;

SELECT schemaname || '.' || relname                        AS table_name,
       to_char(n_live_tup, 'FM999,999,999')                AS live_rows,
       pg_size_pretty(pg_total_relation_size(relid))       AS total_size,
       pg_size_pretty(pg_relation_size(relid))             AS heap_size,
       pg_size_pretty(pg_indexes_size(relid))              AS index_size
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 25;

\echo '===== 3. DEAD TUPLES / VACUUM HEALTH ====='
SELECT schemaname || '.' || relname AS table_name,
       n_live_tup, n_dead_tup,
       CASE WHEN n_live_tup > 0
            THEN round(100.0 * n_dead_tup / n_live_tup, 1) END AS dead_pct,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000
ORDER BY n_dead_tup DESC
LIMIT 20;

\echo '===== 4. INDEX INVENTORY (largest first) ====='
SELECT s.schemaname || '.' || s.relname          AS table_name,
       s.indexrelname                            AS index_name,
       i.indisunique                             AS is_unique,
       s.idx_scan                                AS scans,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS size
FROM pg_stat_user_indexes s
JOIN pg_index i USING (indexrelid)
ORDER BY pg_relation_size(s.indexrelid) DESC
LIMIT 30;

\echo '===== 5. NEVER-SCANNED NON-UNIQUE INDEXES (check replicas before dropping) ====='
SELECT s.schemaname || '.' || s.relname AS table_name,
       s.indexrelname                   AS index_name,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS size
FROM pg_stat_user_indexes s
JOIN pg_index i USING (indexrelid)
WHERE i.indisunique IS FALSE AND s.idx_scan = 0
ORDER BY pg_relation_size(s.indexrelid) DESC
LIMIT 20;

\echo '===== 6. INVALID INDEXES (failed CONCURRENTLY builds) ====='
SELECT c.relname AS index_name
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE NOT i.indisvalid;

\echo '===== 7. FOREIGN KEYS WITHOUT AN INDEX ON THE CHILD COLUMN ====='
SELECT conrelid::regclass AS child_table,
       conname            AS constraint_name,
       (SELECT string_agg(attname, ', ' ORDER BY ord)
        FROM unnest(conkey) WITH ORDINALITY AS u(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = conrelid AND a.attnum = u.attnum) AS child_columns
FROM pg_constraint c
WHERE contype = 'f'
  -- no index whose LEFTMOST column is the first FK column (indkey is 0-based)
  AND NOT EXISTS (
      SELECT 1 FROM pg_index i
      WHERE i.indrelid = c.conrelid
        AND i.indisvalid
        AND i.indkey[0] = c.conkey[1]
  )
ORDER BY 1;

\echo '===== 8. LONG-RUNNING AND IDLE-IN-TRANSACTION SESSIONS ====='
SELECT pid, state, now() - xact_start AS xact_age, now() - query_start AS query_age,
       wait_event_type, left(query, 100) AS query
FROM pg_stat_activity
WHERE state <> 'idle' AND pid <> pg_backend_pid()
ORDER BY xact_start NULLS LAST
LIMIT 20;

\echo '===== 9. BLOCKING CHAINS ====='
SELECT blocked.pid   AS blocked_pid,  left(blocked.query, 60)  AS blocked_query,
       blocking.pid  AS blocking_pid, left(blocking.query, 60) AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0;

\echo '===== 10. TOP STATEMENTS BY TOTAL TIME (needs pg_stat_statements) ====='
SELECT round(total_exec_time::numeric, 1) AS total_ms,
       calls,
       round(mean_exec_time::numeric, 2)  AS mean_ms,
       rows,
       left(regexp_replace(query, '\s+', ' ', 'g'), 120) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;

\echo '===== 11. CACHE HIT RATIO AND SEQ-SCAN-HEAVY TABLES ====='
SELECT round(100.0 * sum(blks_hit) / nullif(sum(blks_hit) + sum(blks_read), 0), 2) AS cache_hit_pct
FROM pg_stat_database WHERE datname = current_database();

SELECT schemaname || '.' || relname AS table_name, seq_scan, idx_scan,
       seq_tup_read, n_live_tup
FROM pg_stat_user_tables
WHERE seq_scan > 0 AND n_live_tup > 100000
ORDER BY seq_tup_read DESC
LIMIT 15;
