-- MySQL 8 read-only snapshot pack.
-- Usage: mysql --defaults-file=~/.my.cnf -t <db> < assets/mysql_snapshot.sql > snapshot.txt
-- Safe on a replica. Every statement is read-only. The sys schema ships with
-- MySQL 8; sections that need it are skipped with an error you can ignore if absent.

SET SESSION TRANSACTION READ ONLY;
SET SESSION max_execution_time = 60000;
SET SESSION lock_wait_timeout = 3;

SELECT '===== 1. VERSION AND KEY SETTINGS =====' AS section;
SELECT VERSION() AS version, @@innodb_version AS innodb_version;
SELECT VARIABLE_NAME, VARIABLE_VALUE
FROM performance_schema.global_variables
WHERE VARIABLE_NAME IN (
  'innodb_buffer_pool_size','innodb_flush_log_at_trx_commit','innodb_io_capacity',
  'innodb_file_per_table','innodb_log_file_size','sort_buffer_size','join_buffer_size',
  'tmp_table_size','max_heap_table_size','max_connections','transaction_isolation',
  'long_query_time','slow_query_log','sql_mode','character_set_server')
ORDER BY VARIABLE_NAME;

SELECT '===== 2. TABLE SIZES AND ROW ESTIMATES =====' AS section;
SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE,
       FORMAT(TABLE_ROWS, 0)                           AS est_rows,
       ROUND(DATA_LENGTH  / 1024 / 1024, 1)            AS data_mb,
       ROUND(INDEX_LENGTH / 1024 / 1024, 1)            AS index_mb,
       ROUND((DATA_LENGTH + INDEX_LENGTH) / 1024 / 1024, 1) AS total_mb
FROM information_schema.TABLES
WHERE TABLE_SCHEMA NOT IN ('mysql','sys','information_schema','performance_schema')
ORDER BY (DATA_LENGTH + INDEX_LENGTH) DESC
LIMIT 25;

SELECT '===== 3. TABLES WITHOUT A PRIMARY KEY =====' AS section;
SELECT t.TABLE_SCHEMA, t.TABLE_NAME
FROM information_schema.TABLES t
LEFT JOIN information_schema.TABLE_CONSTRAINTS c
       ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
      AND c.CONSTRAINT_TYPE = 'PRIMARY KEY'
WHERE t.TABLE_SCHEMA NOT IN ('mysql','sys','information_schema','performance_schema')
  AND t.TABLE_TYPE = 'BASE TABLE'
  AND c.CONSTRAINT_NAME IS NULL;

SELECT '===== 4. INDEX INVENTORY (composite order preserved) =====' AS section;
SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME,
       GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns,
       MAX(NON_UNIQUE) = 0                             AS is_unique,
       MAX(CARDINALITY)                                AS cardinality
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA NOT IN ('mysql','sys','information_schema','performance_schema')
GROUP BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME
ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME;

-- Usually empty on InnoDB: it auto-creates an index for every declared FK.
-- Rows here mean a non-InnoDB table or a dropped index; the real MySQL risk is
-- an UNDECLARED foreign key, which section 4's inventory exposes.
SELECT '===== 5. FOREIGN KEYS WITHOUT AN INDEX ON THE CHILD COLUMN =====' AS section;
SELECT k.TABLE_SCHEMA, k.TABLE_NAME AS child_table, k.COLUMN_NAME AS child_column,
       k.REFERENCED_TABLE_NAME AS parent_table, k.CONSTRAINT_NAME
FROM information_schema.KEY_COLUMN_USAGE k
LEFT JOIN information_schema.STATISTICS s
       ON s.TABLE_SCHEMA = k.TABLE_SCHEMA AND s.TABLE_NAME = k.TABLE_NAME
      AND s.COLUMN_NAME = k.COLUMN_NAME AND s.SEQ_IN_INDEX = 1
WHERE k.REFERENCED_TABLE_NAME IS NOT NULL
  AND k.ORDINAL_POSITION = 1
  AND s.INDEX_NAME IS NULL;

SELECT '===== 6. FK TYPE MISMATCHES (child vs parent column type) =====' AS section;
SELECT k.TABLE_NAME AS child_table, k.COLUMN_NAME AS child_column, cc.COLUMN_TYPE AS child_type,
       k.REFERENCED_TABLE_NAME AS parent_table, k.REFERENCED_COLUMN_NAME AS parent_column,
       pc.COLUMN_TYPE AS parent_type
FROM information_schema.KEY_COLUMN_USAGE k
JOIN information_schema.COLUMNS cc
  ON cc.TABLE_SCHEMA = k.TABLE_SCHEMA AND cc.TABLE_NAME = k.TABLE_NAME
 AND cc.COLUMN_NAME = k.COLUMN_NAME
JOIN information_schema.COLUMNS pc
  ON pc.TABLE_SCHEMA = k.TABLE_SCHEMA AND pc.TABLE_NAME = k.REFERENCED_TABLE_NAME
 AND pc.COLUMN_NAME = k.REFERENCED_COLUMN_NAME
WHERE k.REFERENCED_TABLE_NAME IS NOT NULL
  AND cc.COLUMN_TYPE <> pc.COLUMN_TYPE;

SELECT '===== 7. UNUSED AND REDUNDANT INDEXES (sys schema) =====' AS section;
SELECT * FROM sys.schema_unused_indexes LIMIT 30;
SELECT table_schema, table_name, redundant_index_name, redundant_index_columns,
       dominant_index_name, dominant_index_columns
FROM sys.schema_redundant_indexes LIMIT 30;

SELECT '===== 8. TOP STATEMENTS BY TOTAL LATENCY (sys schema) =====' AS section;
SELECT db, exec_count, total_latency, avg_latency, rows_sent_avg, rows_examined_avg,
       LEFT(query, 120) AS query
FROM sys.statement_analysis
ORDER BY total_latency DESC
LIMIT 20;

SELECT '===== 9. FULL TABLE SCANS (sys schema) =====' AS section;
SELECT * FROM sys.statements_with_full_table_scans LIMIT 15;

SELECT '===== 10. OPEN TRANSACTIONS AND LOCK WAITS =====' AS section;
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_seconds,
       trx_rows_locked, trx_rows_modified, LEFT(trx_query, 100) AS query
FROM information_schema.INNODB_TRX
ORDER BY trx_started
LIMIT 20;

SELECT * FROM performance_schema.data_lock_waits LIMIT 20;

SELECT '===== 11. BUFFER POOL EFFICIENCY =====' AS section;
SELECT VARIABLE_NAME, VARIABLE_VALUE
FROM performance_schema.global_status
WHERE VARIABLE_NAME IN ('Innodb_buffer_pool_read_requests','Innodb_buffer_pool_reads',
                        'Innodb_row_lock_waits','Innodb_row_lock_time_avg',
                        'Created_tmp_disk_tables','Created_tmp_tables',
                        'Select_full_join','Select_scan','Sort_merge_passes');

-- Also run manually and read "History list length" (purge lag) and the
-- "LATEST DETECTED DEADLOCK" section:   SHOW ENGINE INNODB STATUS\G
