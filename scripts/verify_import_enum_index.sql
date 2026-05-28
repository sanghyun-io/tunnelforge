-- TunnelForge v2.0.13 import verification queries.
-- Run this after selecting the imported MySQL database:
--
--   USE dataflare;
--
-- It checks the two previously reported import issues:
-- 1. ENUM literal/value case preservation
-- 2. Secondary index restoration after import

-- 1. All ENUM column definitions in the imported database.
SELECT
  TABLE_NAME,
  COLUMN_NAME,
  COLUMN_TYPE,
  COLUMN_DEFAULT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND DATA_TYPE = 'enum'
ORDER BY TABLE_NAME, COLUMN_NAME;

-- 2. Specific known-problem ENUM definition.
-- Expected COLUMN_TYPE: enum('HIGH','MEDIUM','LOW')
-- Expected COLUMN_DEFAULT: MEDIUM
SELECT
  TABLE_NAME,
  COLUMN_NAME,
  COLUMN_TYPE,
  COLUMN_DEFAULT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'df_evaluations_norm'
  AND COLUMN_NAME = 'importance';

-- 3. Specific known-problem ENUM stored values.
-- HEX() makes case changes visible even under case-insensitive collations.
-- Expected uppercase values: HIGH, MEDIUM, LOW.
-- This old dump had approximately HIGH=423, MEDIUM=1793, LOW=277, EMPTY=54.
SELECT
  CASE
    WHEN importance IS NULL THEN '<NULL>'
    WHEN importance = '' THEN '<EMPTY>'
    ELSE importance
  END AS importance_display,
  HEX(importance) AS importance_hex,
  COUNT(*) AS row_count
FROM df_evaluations_norm
GROUP BY
  CASE
    WHEN importance IS NULL THEN '<NULL>'
    WHEN importance = '' THEN '<EMPTY>'
    ELSE importance
  END,
  HEX(importance)
ORDER BY importance_display;

-- 4. Lowercase ENUM value smoke check.
-- Expected result: zero rows.
SELECT
  importance,
  HEX(importance) AS importance_hex,
  COUNT(*) AS row_count
FROM df_evaluations_norm
WHERE BINARY importance IN ('high', 'medium', 'low')
GROUP BY importance, HEX(importance);

-- 5. All indexes, including PRIMARY.
SELECT
  TABLE_NAME,
  INDEX_NAME,
  CASE WHEN NON_UNIQUE = 0 THEN 'UNIQUE' ELSE 'NON_UNIQUE' END AS uniqueness,
  GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE
ORDER BY TABLE_NAME, INDEX_NAME;

-- 6. Secondary indexes only.
SELECT
  TABLE_NAME,
  INDEX_NAME,
  CASE WHEN NON_UNIQUE = 0 THEN 'UNIQUE' ELSE 'NON_UNIQUE' END AS uniqueness,
  GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND INDEX_NAME <> 'PRIMARY'
GROUP BY TABLE_NAME, INDEX_NAME, NON_UNIQUE
ORDER BY TABLE_NAME, INDEX_NAME;

-- 7. Secondary index count.
-- The provided dump manifest contained 375 secondary indexes.
-- A result of 0, or a count far below the source, means post-data index DDL failed.
SELECT
  COUNT(DISTINCT TABLE_NAME, INDEX_NAME) AS secondary_index_count
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND INDEX_NAME <> 'PRIMARY';

-- 8. Tables with no secondary indexes.
-- This is not always wrong, but useful for spotting unexpectedly empty tables.
SELECT
  t.TABLE_NAME
FROM information_schema.TABLES t
LEFT JOIN information_schema.STATISTICS s
  ON s.TABLE_SCHEMA = t.TABLE_SCHEMA
  AND s.TABLE_NAME = t.TABLE_NAME
  AND s.INDEX_NAME <> 'PRIMARY'
WHERE t.TABLE_SCHEMA = DATABASE()
  AND t.TABLE_TYPE = 'BASE TABLE'
GROUP BY t.TABLE_NAME
HAVING COUNT(s.INDEX_NAME) = 0
ORDER BY t.TABLE_NAME;

-- 9. Known table index detail.
SHOW INDEX FROM df_evaluations_norm;

-- 10. Example execution plan for the known ENUM column.
-- If an index exists and is useful, key should not be NULL.
EXPLAIN
SELECT *
FROM df_evaluations_norm
WHERE importance = 'HIGH';

-- 11. Replace this with the slow application query that previously full-scanned.
-- Expected: type should ideally not be ALL, and key should not be NULL.
-- EXPLAIN
-- SELECT *
-- FROM your_table
-- WHERE col_a = 'value'
--   AND col_b = 'value';
