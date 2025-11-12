# Database Architect Review - PSGC Database Schema
**Date:** 2025-11-12 11:10
**Reviewer:** PostgreSQL/PostGIS Specialist
**Scope:** schema.sql, database design, performance optimization

## Executive Summary
The PSGC schema demonstrates solid foundational design with proper normalization, self-referencing hierarchy, and PostGIS integration. However, it requires critical indexing improvements, constraint hardening, and deployment safety enhancements before production use. The truncate-and-reload pattern poses concurrency risks for a database serving live queries, and several missing indexes will cause severe performance degradation at scale.

## Strengths
Observed best practices in current schema design:

- **Proper normalization**: Spine table (`locations`) + attribute tables pattern eliminates sparse columns and enforces single responsibility (schema.sql:64-118)
- **Self-referencing FK with cascade control**: `parent_psgc REFERENCES locations(psgc_code) ON UPDATE CASCADE ON DELETE RESTRICT` prevents orphaned children and enables standard recursive CTEs (schema.sql:68)
- **Reference tables as enums**: `geographic_levels`, `city_class_types`, `income_brackets`, `urban_rural_tags` provide data integrity via FK constraints while remaining human-readable (schema.sql:5-61)
- **Idempotent DDL**: All `CREATE TABLE IF NOT EXISTS` and `INSERT...ON CONFLICT DO NOTHING` patterns enable safe schema reapplication (schema.sql:5, 10, 26, etc.)
- **PostGIS readiness**: `GEOMETRY(MultiPolygon, 4326)` column with GIST index prepared for future spatial data (schema.sql:74, 81)
- **Unique constraint on population measurements**: `(psgc_code, reference_year, source)` prevents duplicate entries from same source/year (schema.sql:91)
- **Non-negative population check**: `CHECK (population >= 0)` prevents data quality issues (schema.sql:88)
- **Timezone-aware timestamps**: `release_notes.created_at` uses `TIMESTAMP WITH TIME ZONE` (schema.sql:126)

## Critical Issues

### Issue 1: Missing Composite Index for Hierarchical Queries
- **Location:** schema.sql:80 (only single-column index exists)
- **Impact:** Critical
- **Description:** The most common query pattern for this system is "find all children of parent X at level Y" (e.g., "all cities in province 1234000000"). Currently only `idx_locations_parent` exists on `parent_psgc` alone. Queries filtering by both parent and level require a sequential scan on the index results. For 42,046 barangays, this becomes a bottleneck.
- **Recommendation:**
```sql
-- Add composite index for parent + level queries
CREATE INDEX IF NOT EXISTS idx_locations_parent_level
ON locations(parent_psgc, level_code)
WHERE parent_psgc IS NOT NULL;

-- Keep existing single-column index for pure parent lookups
-- idx_locations_parent remains useful for "all descendants" queries
```
- **Example Query Impact:**
```sql
-- Current: Index scan on parent_psgc, then filter on level_code (slower)
-- With composite index: Direct index-only scan (10-100x faster)
SELECT name, psgc_code
FROM locations
WHERE parent_psgc = '1234000000' AND level_code = 'City';
```

### Issue 2: No Index on locations.level_code
- **Location:** schema.sql:67 (level_code column has no index)
- **Impact:** High
- **Description:** All aggregate queries filtering by geographic level (provinces, cities, barangays) perform sequential scans. With 42,046 barangays in 43,769 total rows, a query for "all provinces" scans 99% irrelevant rows.
- **Recommendation:**
```sql
-- Partial indexes for common level queries
CREATE INDEX IF NOT EXISTS idx_locations_level_prov
ON locations(level_code, name)
WHERE level_code = 'Prov';

CREATE INDEX IF NOT EXISTS idx_locations_level_city
ON locations(level_code, name)
WHERE level_code IN ('City', 'Mun');

CREATE INDEX IF NOT EXISTS idx_locations_level_bgy
ON locations(level_code, parent_psgc)
WHERE level_code = 'Bgy';

-- General index for other levels and mixed queries
CREATE INDEX IF NOT EXISTS idx_locations_level
ON locations(level_code);
```
- **Example Query Impact:**
```sql
-- Without index: Sequential scan of 43,769 rows to find 82 provinces
-- With partial index: Direct lookup, returns 82 rows immediately
SELECT name, psgc_code FROM locations WHERE level_code = 'Prov';
```

### Issue 3: Missing Index on population_stats.psgc_code
- **Location:** schema.sql:86 (FK without supporting index)
- **Impact:** Critical
- **Description:** Every join from `locations` to `population_stats` (the primary use case) requires a sequential scan of population_stats. With 43,768 population rows, joins become O(n²) operations. PostgreSQL does NOT automatically create indexes on foreign key columns (only the referenced column).
- **Recommendation:**
```sql
-- Critical: Index on FK column for efficient joins
CREATE INDEX IF NOT EXISTS idx_population_stats_psgc
ON population_stats(psgc_code);

-- Composite index for year + location queries
CREATE INDEX IF NOT EXISTS idx_population_stats_psgc_year
ON population_stats(psgc_code, reference_year);
```
- **Example Query Impact:**
```sql
-- Top 5 provinces by population (most common query pattern)
-- Without idx_population_stats_psgc: Hash join with seq scan (50-200ms)
-- With index: Nested loop with index scan (5-15ms)
SELECT l.name, ps.population
FROM population_stats ps
JOIN locations l ON l.psgc_code = ps.psgc_code
WHERE ps.reference_year = 2024 AND l.level_code = 'Prov'
ORDER BY ps.population DESC LIMIT 5;
```

### Issue 4: Truncate-and-Reload Concurrency Hazard
- **Location:** deploy_to_db.py:64 (`TRUNCATE TABLE {table} CASCADE`)
- **Impact:** Critical
- **Description:** `TRUNCATE` acquires `ACCESS EXCLUSIVE` lock, blocking all concurrent readers. On Neon (serverless Postgres), active queries will fail with "relation does not exist" errors during the truncate window. The CASCADE modifier propagates to child tables, amplifying the outage window.
- **Recommendation:** Implement transaction-based atomic swap:
```sql
-- Instead of truncate-and-reload, use transactional replacement
BEGIN;

-- Create staging tables (schema.sql should include staging versions)
CREATE TEMP TABLE locations_staging (LIKE locations INCLUDING ALL);
CREATE TEMP TABLE population_stats_staging (LIKE population_stats INCLUDING ALL);
-- ... other tables

-- COPY into staging tables (no locks on production tables)
COPY locations_staging FROM STDIN WITH (FORMAT csv, HEADER true);
-- ... load all staging tables

-- Atomic swap (brief exclusive lock, <100ms)
ALTER TABLE locations RENAME TO locations_old;
ALTER TABLE locations_staging RENAME TO locations;

ALTER TABLE population_stats RENAME TO population_stats_old;
ALTER TABLE population_stats_staging RENAME TO population_stats;

-- Drop old tables outside transaction to avoid bloating
COMMIT;
DROP TABLE locations_old CASCADE;
```
**Alternative (simpler):** Use `DELETE` instead of `TRUNCATE` for smaller lock duration:
```sql
-- DELETE allows concurrent reads on uncommitted data (MVCC)
BEGIN;
DELETE FROM locations; -- Row-level locks, not table-level
COPY locations FROM STDIN WITH (FORMAT csv, HEADER true);
COMMIT;
```

### Issue 5: Premature Spatial Index
- **Location:** schema.sql:81 (`idx_locations_geom`)
- **Impact:** Medium
- **Description:** Creating a GIST spatial index on an entirely NULL column wastes storage (index still consumes ~1-2 MB) and adds overhead to every INSERT/UPDATE. PostgreSQL must maintain index structure even for NULL values.
- **Recommendation:** Remove index from schema.sql, document creation for later:
```sql
-- Remove from schema.sql:81
-- CREATE INDEX IF NOT EXISTS idx_locations_geom ON locations USING GIST (geom);

-- Add to migration script when geometries are loaded:
-- CREATE INDEX CONCURRENTLY idx_locations_geom ON locations USING GIST (geom)
-- WHERE geom IS NOT NULL;
```

### Issue 6: Missing Constraints on PSGC Code Format
- **Location:** schema.sql:65 (psgc_code defined as CHAR(10) without validation)
- **Impact:** Medium
- **Description:** ETL normalizes codes to 10-digit zero-padded format (etl_psgc.py:23), but schema doesn't enforce this. Invalid codes could be inserted via direct SQL, breaking parent inference logic and queries that rely on substring operations.
- **Recommendation:**
```sql
-- Add check constraint for 10-digit format
ALTER TABLE locations
ADD CONSTRAINT chk_psgc_code_format
CHECK (psgc_code ~ '^\d{10}$');

-- Add similar constraints to attribute tables
ALTER TABLE population_stats
ADD CONSTRAINT chk_psgc_code_format
CHECK (psgc_code ~ '^\d{10}$');

ALTER TABLE city_classifications
ADD CONSTRAINT chk_psgc_code_format
CHECK (psgc_code ~ '^\d{10}$');

ALTER TABLE income_classifications
ADD CONSTRAINT chk_psgc_code_format
CHECK (psgc_code ~ '^\d{10}$');

ALTER TABLE settlement_tags
ADD CONSTRAINT chk_psgc_code_format
CHECK (psgc_code ~ '^\d{10}$');
```

### Issue 7: No Index on locations.name for Search
- **Location:** schema.sql:66 (name column unindexed)
- **Impact:** Medium
- **Description:** User-facing search queries ("find all locations named 'Manila'") or autocomplete features require full table scans. Filipino location names with diacritics may need specialized collation handling.
- **Recommendation:**
```sql
-- B-tree index for exact matches and range queries (sorted results)
CREATE INDEX IF NOT EXISTS idx_locations_name
ON locations(name);

-- Optional: Trigram index for fuzzy search (requires pg_trgm extension)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_locations_name_trgm
ON locations USING gin (name gin_trgm_ops);

-- Usage example for fuzzy search:
-- SELECT name FROM locations WHERE name % 'Maynila' ORDER BY similarity(name, 'Maynila') DESC LIMIT 10;
```

## Indexing Recommendations

### For Hierarchical Queries
```sql
-- Composite index for "all children of parent X at level Y"
CREATE INDEX IF NOT EXISTS idx_locations_parent_level
ON locations(parent_psgc, level_code)
WHERE parent_psgc IS NOT NULL;

-- Rationale: Enables index-only scans for queries like "all cities in province X"
-- Covers common pattern: parent filtering + level filtering
-- WHERE clause excludes root regions (parent_psgc IS NULL), reducing index size by ~17 rows

-- Covering index for hierarchy traversal with names
CREATE INDEX IF NOT EXISTS idx_locations_parent_covering
ON locations(parent_psgc, level_code, name, psgc_code)
WHERE parent_psgc IS NOT NULL;

-- Rationale: Index-only scan eliminates heap access for common hierarchy queries
-- Useful for UI components showing location lists without needing full row data
```

### For Population Analytics
```sql
-- Critical: Foreign key index for joins
CREATE INDEX IF NOT EXISTS idx_population_stats_psgc
ON population_stats(psgc_code);

-- Rationale: Enables efficient nested loop joins from locations to population_stats
-- Without this, every join is O(n²) sequential scan

-- Composite for year-specific queries
CREATE INDEX IF NOT EXISTS idx_population_stats_psgc_year
ON population_stats(psgc_code, reference_year);

-- Rationale: Covers filtering by year, common in "latest population" queries

-- Descending index for top-N queries
CREATE INDEX IF NOT EXISTS idx_population_stats_pop_desc
ON population_stats(reference_year, population DESC)
WHERE reference_year >= 2020;

-- Rationale: Optimizes ORDER BY population DESC LIMIT N queries
-- WHERE clause limits to recent years, reducing index size
-- Example: "Top 10 most populous locations in 2024"

-- Covering index for dashboard queries
CREATE INDEX IF NOT EXISTS idx_population_stats_covering
ON population_stats(reference_year, population DESC, psgc_code, source)
WHERE reference_year >= 2020;

-- Rationale: Index-only scan for complete population rankings without heap access
```

### For Spatial Queries (Future)
```sql
-- Defer spatial index until geometries are loaded
-- DO NOT create now (current implementation creates index on all-NULL column)

-- When geometries are available, create conditionally:
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_geom
ON locations USING GIST (geom)
WHERE geom IS NOT NULL;

-- Rationale:
-- - CONCURRENTLY avoids blocking concurrent queries during creation
-- - WHERE geom IS NOT NULL excludes ~40k NULL rows if partial geometry data
-- - GIST is appropriate for polygon containment/intersection queries

-- Advanced: Spatial index with custom parameters for Philippines geography
CREATE INDEX IF NOT EXISTS idx_locations_geom_tuned
ON locations USING GIST (geom)
WITH (fillfactor = 90);

-- Rationale: fillfactor=90 leaves 10% free space for future geometry updates
-- Default is 90 for GIST, but explicit is clearer for documentation

-- Spatial index for specific geometry types if mixed
CREATE INDEX IF NOT EXISTS idx_locations_geom_points
ON locations USING GIST (geom)
WHERE GeometryType(geom) = 'POINT';

-- Rationale: If barangays are stored as points vs polygons, separate indexes improve query planning
```

### For Name Search and Autocomplete
```sql
-- Standard B-tree for exact matches
CREATE INDEX IF NOT EXISTS idx_locations_name
ON locations(name);

-- Rationale: Supports exact lookups, prefix searches (name LIKE 'Manila%')

-- Composite for level-scoped searches
CREATE INDEX IF NOT EXISTS idx_locations_level_name
ON locations(level_code, name);

-- Rationale: "Find all cities named X" filters level first, reducing search space

-- Case-insensitive search
CREATE INDEX IF NOT EXISTS idx_locations_name_lower
ON locations(LOWER(name));

-- Rationale: Enables case-insensitive lookups without ILIKE (which doesn't use indexes)
-- Query: SELECT * FROM locations WHERE LOWER(name) = LOWER('manila')

-- Fuzzy search with trigrams (optional, requires pg_trgm)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_locations_name_trgm
ON locations USING gin (name gin_trgm_ops);

-- Rationale: Supports similarity searches for misspellings or partial matches
-- Query: SELECT * FROM locations WHERE name % 'Maynila' ORDER BY similarity(name, 'Maynila') DESC;
```

## Schema Improvements

### Normalization Adjustments
**Current design is appropriate** for PSGC use case. Alternative normalization considered but rejected:

**Option A: Combine city_classifications and income_classifications**
```sql
-- NOT RECOMMENDED: Creates sparsity (most cities have no income class)
CREATE TABLE location_attributes (
    psgc_code CHAR(10) PRIMARY KEY,
    city_class TEXT REFERENCES city_class_types(class_code),
    income_bracket TEXT REFERENCES income_brackets(bracket_code),
    ...
);
```
**Rejection rationale:** Current separate tables are better because:
- City class applies only to 143 cities
- Income class applies to ~1,724 municipalities/cities/provinces
- Sparse table would have 42,000+ rows with NULL city_class
- Separate tables keep attribute semantics clear

**Option B: Closure Table for Hierarchy**
```sql
-- NOT RECOMMENDED for PSGC: Adds complexity without clear benefit
CREATE TABLE location_hierarchy (
    ancestor CHAR(10) REFERENCES locations(psgc_code),
    descendant CHAR(10) REFERENCES locations(psgc_code),
    depth SMALLINT,
    PRIMARY KEY (ancestor, descendant)
);
```
**Rejection rationale:**
- PSGC hierarchy is shallow (max 5 levels: Reg→Prov→City→SubMun→Bgy)
- Recursive CTEs perform adequately for shallow hierarchies (<1000 recursion depth)
- Closure table adds ~220k rows (43,769 locations × avg 5 ancestors) for marginal gain
- Trade-off: faster reads, but 5x storage and complex INSERT/UPDATE logic

**Recommendation:** Keep current normalization. Monitor recursive CTE performance; migrate to closure table only if queries exceed 200ms.

### Constraint Additions
```sql
-- 1. PSGC code format validation (critical)
ALTER TABLE locations
ADD CONSTRAINT chk_psgc_code_format
CHECK (psgc_code ~ '^\d{10}$');

-- Rationale: Enforces ETL contract, prevents manual insertion errors

-- 2. Reference year range validation
ALTER TABLE population_stats
ADD CONSTRAINT chk_reference_year_range
CHECK (reference_year BETWEEN 1900 AND 2100);

-- Rationale: Catches typos (e.g., 20024 instead of 2024)

ALTER TABLE income_classifications
ADD CONSTRAINT chk_effective_year_range
CHECK (effective_year IS NULL OR effective_year BETWEEN 1900 AND 2100);

ALTER TABLE settlement_tags
ADD CONSTRAINT chk_reference_year_range
CHECK (reference_year IS NULL OR reference_year BETWEEN 1900 AND 2100);

-- 3. Name non-empty validation
ALTER TABLE locations
ADD CONSTRAINT chk_name_not_empty
CHECK (TRIM(name) <> '');

-- Rationale: Prevents empty/whitespace-only names from ETL errors

-- 4. Ensure level_code is from known set (redundant with FK, but faster to check)
-- Current design is correct: FK to geographic_levels is sufficient

-- 5. Population source non-empty
ALTER TABLE population_stats
ADD CONSTRAINT chk_source_not_empty
CHECK (TRIM(source) <> '');

-- Rationale: Source attribution is critical for data provenance

-- 6. Prevent self-referencing parents (data integrity)
ALTER TABLE locations
ADD CONSTRAINT chk_no_self_parent
CHECK (parent_psgc IS NULL OR parent_psgc <> psgc_code);

-- Rationale: Breaks recursive queries, should never occur in valid PSGC data

-- 7. Effective/retirement date logic
ALTER TABLE locations
ADD CONSTRAINT chk_dates_logical
CHECK (retired_date IS NULL OR effective_date IS NULL OR retired_date > effective_date);

-- Rationale: Retirement must come after creation
```

### Partitioning Strategy
**Table:** `population_stats` (candidate for partitioning)

**Analysis:**
- Current data: 43,768 rows for 2024 only
- Growth: +43,768 rows/year (assuming annual census continues)
- 10-year projection: ~440k rows (manageable without partitioning)
- 50-year projection: ~2.2M rows (partitioning beneficial)

**Recommendation:** Defer partitioning until year 2030 or 500k rows, whichever comes first.

**When to partition:**
```sql
-- Range partitioning by reference_year (future implementation)
CREATE TABLE population_stats (
    population_id BIGSERIAL NOT NULL,
    psgc_code CHAR(10) NOT NULL REFERENCES locations(psgc_code) ON DELETE CASCADE,
    reference_year SMALLINT NOT NULL,
    population BIGINT NOT NULL CHECK (population >= 0),
    source TEXT NOT NULL,
    collected_at DATE DEFAULT CURRENT_DATE,
    PRIMARY KEY (psgc_code, reference_year, source)
) PARTITION BY RANGE (reference_year);

-- Create partitions per decade
CREATE TABLE population_stats_2020s PARTITION OF population_stats
    FOR VALUES FROM (2020) TO (2030);

CREATE TABLE population_stats_2030s PARTITION OF population_stats
    FOR VALUES FROM (2030) TO (2040);

-- Benefits after partitioning:
-- - Queries filtering by year scan only relevant partition
-- - Old partitions can be archived to cheaper storage
-- - VACUUM/ANALYZE operations are partition-scoped (faster)

-- Drawbacks:
-- - Adds DDL complexity for each new decade
-- - Foreign key constraints more complex
-- - Limited benefit with current 1-year dataset

-- Threshold: Implement when query times exceed 100ms or table exceeds 500k rows
```

**Other tables:** No partitioning needed.
- `locations`: Static size (~45k rows, no time dimension)
- Attribute tables: Small (<2k rows each)

## PostGIS Optimization

### Geometry Column Design
**Current:** `geom GEOMETRY(MultiPolygon, 4326)` (schema.sql:74)

**Assessment:**
- **MultiPolygon is correct for provinces/cities/municipalities** (may have islands/exclaves)
- **Not optimal for barangays** (most are contiguous single polygons)
- **Not optimal for points-of-interest** (city halls, capitals)

**Recommendation:** Use inheritance or typed column approach:

**Option A: Single column with mixed types (current approach - KEEP)**
```sql
-- Keep current schema, allow mixed geometry types
ALTER TABLE locations ALTER COLUMN geom TYPE GEOMETRY(Geometry, 4326);

-- Rationale: Simplifies queries, PostgreSQL handles mixed types efficiently
-- Trade-off: Slightly larger index, but uniform query interface
```

**Option B: Separate geometry columns by type (NOT recommended)**
```sql
-- Don't do this - adds query complexity
geom_polygon GEOMETRY(MultiPolygon, 4326),
geom_point GEOMETRY(Point, 4326),
geom_line GEOMETRY(LineString, 4326)
```

**Option C: Separate geometry table (recommended for large geometries)**
```sql
-- If geometries exceed 1MB each (e.g., high-resolution coastlines)
CREATE TABLE location_geometries (
    psgc_code CHAR(10) PRIMARY KEY REFERENCES locations(psgc_code) ON DELETE CASCADE,
    geom GEOMETRY(Geometry, 4326),
    simplified_geom GEOMETRY(Geometry, 4326), -- For zoom levels < 10
    source TEXT,
    resolution_meters NUMERIC(10,2),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_location_geom ON location_geometries USING GIST (geom);
CREATE INDEX idx_location_geom_simplified ON location_geometries USING GIST (simplified_geom);

-- Rationale:
-- - Keeps locations table lightweight for non-spatial queries
-- - Allows multiple geometry representations (full vs simplified)
-- - Enables geometry-specific metadata (source, resolution)
```

**Final recommendation:** Keep current single-column approach until geometry sizes exceed 100KB average. Then migrate to separate geometry table.

### Spatial Index Tuning
```sql
-- Remove premature index from schema.sql:81
-- CREATE INDEX IF NOT EXISTS idx_locations_geom ON locations USING GIST (geom);

-- Replace with conditional index when geometries are loaded
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_geom
ON locations USING GIST (geom)
WHERE geom IS NOT NULL;

-- Advanced tuning for Philippines geography (when geometries loaded):

-- 1. Partial index for regions only (larger geometries, fewer rows)
CREATE INDEX idx_locations_geom_regions
ON locations USING GIST (geom)
WHERE level_code = 'Reg' AND geom IS NOT NULL;

-- Rationale: Region-level spatial queries (e.g., "which region contains this point")
-- only need to scan 17 geometries, not 43k

-- 2. Partial index for barangays (smaller geometries, most rows)
CREATE INDEX idx_locations_geom_barangays
ON locations USING GIST (geom)
WHERE level_code = 'Bgy' AND geom IS NOT NULL;

-- Rationale: Barangay-level queries (e.g., "nearest barangay to GPS coordinate")
-- benefit from dedicated index without province/region overhead

-- 3. GIST index parameters for Philippines
CREATE INDEX idx_locations_geom_tuned
ON locations USING GIST (geom gist_geometry_ops_2d)
WITH (fillfactor = 90, buffering = auto);

-- Rationale:
-- - fillfactor=90: Leaves space for future updates (geometry corrections)
-- - buffering=auto: Optimizes index build for mixed geometry sizes
-- - gist_geometry_ops_2d: Explicit operator class (2D operations only, no Z/M)

-- 4. Include spatial statistics
ANALYZE locations; -- Updates statistics for query planner

-- After geometry load, update planner statistics:
ALTER TABLE locations
ALTER COLUMN geom SET STATISTICS 1000;

-- Rationale: Default statistics target (100) may be insufficient for 43k diverse geometries
-- Increased statistics improve query planner accuracy for spatial joins
```

### SRID Considerations
**Current:** SRID 4326 (WGS84 geographic coordinates)

**Assessment for Philippines:**
- **4326 (WGS84) - Current choice**: Global standard, GPS-compatible, Web Mercator friendly
  - **Pros:** Universal compatibility, PostGIS default, works with Mapbox/Leaflet/Google Maps
  - **Cons:** Distance/area calculations in degrees, not meters (requires ST_Transform or geography type)

- **3123 (PRS92 / Philippines Reference System 1992)**: National projected coordinate system
  - **Pros:** Accurate distance/area for Philippines, meters-based
  - **Cons:** Distorts at ~500km from central meridian (121°E), poor for Palawan

- **32651 (WGS84 / UTM Zone 51N)**: Covers most of Philippines (120-126°E)
  - **Pros:** Meters-based, low distortion for central/eastern Philippines
  - **Cons:** Western Philippines (Palawan) in Zone 50N, requires multiple SRIDs

**Recommendation:** Keep SRID 4326, but add `geography` type for accurate measurements:

```sql
-- Option A: Add geography column for distance/area calculations (RECOMMENDED)
ALTER TABLE locations
ADD COLUMN geog GEOGRAPHY(MultiPolygon, 4326);

-- Populate from geometry column
UPDATE locations
SET geog = geom::geography
WHERE geom IS NOT NULL;

-- Index the geography column
CREATE INDEX idx_locations_geog
ON locations USING GIST (geog);

-- Rationale:
-- - geography type uses spheroid calculations (accurate to 0.3% globally)
-- - ST_Distance(geog1, geog2) returns meters, not degrees
-- - Coexists with geometry column: use geometry for display, geography for measurement

-- Usage examples:
-- Distance in meters: SELECT ST_Distance(geog1, geog2) FROM ...
-- Area in sq meters: SELECT ST_Area(geog) FROM locations WHERE level_code = 'Prov'
-- Within radius: SELECT * FROM locations WHERE ST_DWithin(geog, point, 5000) -- 5km
```

**Option B:** Keep 4326, transform on-the-fly for measurements:
```sql
-- Transform to PRS92 for calculations (less efficient)
SELECT ST_Area(ST_Transform(geom, 3123)) AS area_sqm
FROM locations WHERE level_code = 'Prov';

-- Rationale: Acceptable for ad-hoc queries, but slower than geography type
```

**Final recommendation:**
1. Keep `geom GEOMETRY(MultiPolygon, 4326)` for storage/display
2. Add `geog GEOGRAPHY(MultiPolygon, 4326)` when geometries loaded
3. Use geometry for visualization, geography for distance/area calculations

## Operational Recommendations

### Deployment Safety
**Critical issue:** Current truncate-and-reload breaks active queries (deploy_to_db.py:64)

**Recommended approach:**

**Level 1: Transactional DELETE (immediate fix)**
```python
# deploy_to_db.py:64 - Replace TRUNCATE with DELETE
def copy_csv(conninfo: str, table: str, csv_path: Path) -> None:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found for {table}: {csv_path}")
    print(f"Loading {table} from {csv_path}...")
    with psycopg.connect(conninfo) as conn:  # Remove autocommit=True
        with conn.cursor() as cur, csv_path.open("r", encoding="utf-8") as fh:
            # Use DELETE instead of TRUNCATE (row-level locks, allows concurrent reads)
            cur.execute(f"DELETE FROM {table}")
            columns = COPY_COLUMNS.get(table)
            column_sql = f"({', '.join(columns)})" if columns else ""
            with cur.copy(
                f"COPY {table} {column_sql} FROM STDIN WITH (FORMAT csv, HEADER true)"
            ) as copy:
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    copy.write(chunk)
        conn.commit()  # Explicit commit after all tables loaded
    print(f"{table} loaded.")

# Rationale:
# - DELETE uses MVCC, old data visible to concurrent transactions until commit
# - Downside: Slower than TRUNCATE (must scan all rows), creates dead tuples
# - Requires VACUUM after load: conn.execute("VACUUM ANALYZE {table}")
```

**Level 2: Blue-Green Deployment (production-grade)**
```python
def deploy_blue_green(conninfo: str, tables: list[str], output_dir: Path) -> None:
    """
    Atomic schema swap using staging tables.
    Zero-downtime deployment with <100ms exclusive lock window.
    """
    with psycopg.connect(conninfo) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            # 1. Create staging tables (no lock on production)
            for table in tables:
                cur.execute(f"""
                    DROP TABLE IF EXISTS {table}_staging CASCADE;
                    CREATE TABLE {table}_staging
                    (LIKE {table} INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
                """)

            # 2. Load into staging (production tables unaffected)
            for table in tables:
                csv_path = output_dir / f"{table}.csv"
                with csv_path.open("r", encoding="utf-8") as fh:
                    columns = COPY_COLUMNS.get(table)
                    column_sql = f"({', '.join(columns)})" if columns else ""
                    with cur.copy(
                        f"COPY {table}_staging {column_sql} FROM STDIN WITH (FORMAT csv, HEADER true)"
                    ) as copy:
                        while chunk := fh.read(CHUNK_SIZE):
                            copy.write(chunk)

            # 3. Validate staging data (optional)
            for table in tables:
                cur.execute(f"SELECT COUNT(*) FROM {table}_staging")
                count = cur.fetchone()[0]
                print(f"Staging {table}: {count} rows")

            # 4. Atomic swap (brief exclusive lock, ~50-100ms)
            for table in tables:
                cur.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
                cur.execute(f"ALTER TABLE {table}_staging RENAME TO {table}")

            conn.commit()

            # 5. Drop old tables outside transaction (avoids bloating commit)
            conn.autocommit = True
            for table in tables:
                cur.execute(f"DROP TABLE IF EXISTS {table}_old CASCADE")

    print("Blue-green deployment complete.")

# Rationale:
# - Staging tables isolated from production
# - Atomic rename operations (metadata-only, ~1ms each)
# - Old data remains queryable until commit
# - Rollback possible until commit
# - Trade-off: 2x temporary storage during deployment
```

**Level 3: Logical Replication (future, for HA setups)**
```sql
-- For multi-region deployments or read replicas
-- Use logical replication to stream changes instead of batch loads
-- Not needed for current single-instance Neon deployment
```

**Immediate action:** Implement Level 1 (transactional DELETE) in next deployment. Plan Level 2 (blue-green) before exposing database to production users.

### Monitoring & Performance
**Key metrics to track:**

**Query Performance Monitoring:**
```sql
-- Enable pg_stat_statements (if not already enabled on Neon)
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Top 10 slowest queries
SELECT
    substring(query, 1, 100) AS short_query,
    calls,
    total_exec_time,
    mean_exec_time,
    max_exec_time,
    stddev_exec_time
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;

-- Queries with high row counts (potential missing indexes)
SELECT
    substring(query, 1, 100) AS short_query,
    calls,
    rows / calls AS avg_rows_per_call,
    mean_exec_time
FROM pg_stat_statements
WHERE calls > 10
ORDER BY rows / calls DESC
LIMIT 10;

-- Cache hit ratio (should be > 99%)
SELECT
    sum(blks_hit) / nullif(sum(blks_hit) + sum(blks_read), 0) AS cache_hit_ratio
FROM pg_stat_database
WHERE datname = 'philippine_standard_geographic_code';
```

**Index Usage Monitoring:**
```sql
-- Unused indexes (candidates for removal)
SELECT
    schemaname,
    tablename,
    indexname,
    idx_scan AS index_scans,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE idx_scan = 0
    AND schemaname = 'public'
ORDER BY pg_relation_size(indexrelid) DESC;

-- Missing indexes (tables with high seq scans)
SELECT
    schemaname,
    tablename,
    seq_scan,
    seq_tup_read,
    idx_scan,
    seq_tup_read / nullif(seq_scan, 0) AS avg_seq_tuples
FROM pg_stat_user_tables
WHERE seq_scan > 1000
    AND schemaname = 'public'
ORDER BY seq_scan DESC;
```

**Table Bloat Monitoring:**
```sql
-- Dead tuple ratio (should trigger VACUUM if > 20%)
SELECT
    schemaname,
    tablename,
    n_live_tup,
    n_dead_tup,
    n_dead_tup::float / nullif(n_live_tup, 0) AS dead_ratio,
    last_vacuum,
    last_autovacuum
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY n_dead_tup DESC;

-- Table/index sizes
SELECT
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS total_size,
    pg_size_pretty(pg_relation_size(schemaname||'.'||tablename)) AS table_size,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename) - pg_relation_size(schemaname||'.'||tablename)) AS index_size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

**Recommended Monitoring Setup:**
1. **Metrics collection:** Neon's built-in monitoring + custom query dashboard
2. **Alerts:**
   - Query latency > 200ms (p95)
   - Cache hit ratio < 95%
   - Dead tuple ratio > 30%
   - Unused indexes consuming > 10MB
3. **Scheduled tasks:**
   - Daily VACUUM ANALYZE after ETL load
   - Weekly review of pg_stat_statements
   - Monthly index usage audit

### Neon-Specific Optimizations
**Serverless Postgres considerations:**

**Connection Pooling:**
```python
# Current deployment uses direct connections
# For production with multiple clients, use connection pooling

# Option A: PgBouncer (external, recommended for Neon)
# Add pgbouncer layer between app and Neon
# Connection string: postgresql://user:pass@pgbouncer-host/db

# Option B: Neon's built-in pooling (if available)
# Check Neon console for pooled connection string
# Example: postgresql://...neon...?pooler=true

# deploy_to_db.py modification for pooled connections:
def apply_schema(conninfo: str, schema_file: Path) -> None:
    # Schema DDL requires direct connection (not pooled)
    direct_conn = conninfo.replace('?pooler=true', '')
    with psycopg.connect(direct_conn, autocommit=True) as conn:
        sql = schema_file.read_text()
        conn.cursor().execute(sql)

def copy_csv(conninfo: str, table: str, csv_path: Path) -> None:
    # COPY can use pooled connection
    with psycopg.connect(conninfo) as conn:
        # ... copy logic
```

**Autoscaling Awareness:**
```sql
-- Neon autoscales compute based on demand
-- Optimize for cold starts (index-only scans reduce I/O)

-- Set statement timeout to prevent runaway queries during scale-up
ALTER DATABASE philippine_standard_geographic_code
SET statement_timeout = '30s';

-- Reduce planner cost estimates for Neon's fast storage
ALTER DATABASE philippine_standard_geographic_code
SET random_page_cost = 1.1;  -- Default 4.0, but Neon uses SSD

-- Enable parallel query (if Neon supports, check max_parallel_workers)
ALTER DATABASE philippine_standard_geographic_code
SET max_parallel_workers_per_gather = 4;
```

**Storage Efficiency:**
```sql
-- Neon charges for storage, optimize table size

-- 1. Use TOAST compression for old_names TEXT column (variable-length data)
ALTER TABLE locations
ALTER COLUMN old_names SET STORAGE EXTENDED; -- Default, but explicit

-- 2. Reduce toast threshold for large text fields (forces out-of-line storage)
ALTER TABLE locations
SET (toast_tuple_target = 2048); -- Default 2048, smaller = more aggressive TOAST

-- 3. Enable table compression (PostgreSQL 14+, check if Neon supports)
-- Note: Neon may handle this transparently at storage layer

-- 4. Periodic VACUUM FULL to reclaim space (only after major deletions)
-- CAUTION: VACUUM FULL rewrites entire table, takes exclusive lock
-- Schedule during maintenance window: VACUUM FULL locations;
```

**Backup Configuration:**
```sql
-- Neon handles automated backups, but verify settings:
-- - Point-in-time recovery (PITR) enabled
-- - Retention period ≥ 7 days
-- - Verify restore process quarterly

-- Application-level backup (export CSVs):
-- Already handled by data_exports/ folder in codebase
-- Ensure data_exports/ is committed to git or backed up separately
```

## Questions for Maintainers

1. **Query patterns:** What percentage of queries are:
   - Hierarchical traversals (parent → children)?
   - Level-specific filters (all provinces, all cities)?
   - Population rankings (top-N)?
   - Name searches?
   - Spatial queries (when geometries loaded)?

   **Impact:** Prioritizes which indexes to create first.

2. **Concurrent usage:** Expected concurrent users/queries when database is exposed via API?
   - < 10 concurrent: Current direct connection acceptable
   - 10-100 concurrent: Need PgBouncer connection pooling
   - 100+ concurrent: Need read replicas + load balancing

   **Impact:** Determines deployment architecture.

3. **Data refresh frequency:** How often will ETL run?
   - Quarterly (aligned with PSA releases): Current truncate-and-reload acceptable with off-peak scheduling
   - Daily/weekly: Need blue-green deployment to avoid downtime
   - Real-time: Need streaming updates (not batch)

   **Impact:** Deployment strategy selection.

4. **Historical data retention:** Will population_stats keep all historical years or just latest?
   - Latest only: No partitioning needed
   - All history since 1990: Partitioning beneficial after ~10 years

   **Impact:** Partitioning decision timeline.

5. **Geometry data timeline:** When will PostGIS geometries be loaded?
   - Next quarter: Prepare spatial indexes now
   - Next year: Defer spatial index, remove from schema.sql
   - No timeline: Remove geom column, add later via migration

   **Impact:** Whether to include geom column in initial schema.

6. **Write access control:** Will database have:
   - ETL-only writes (current): Simple security model
   - Application writes (user-contributed corrections): Need row-level security, audit logging
   - Public writes: Need extensive validation, rate limiting

   **Impact:** Security and constraint requirements.

7. **API layer:** Planned API technology?
   - PostgREST: Need views for complex queries, RLS for security
   - Hasura: Need to verify query performance with Hasura's SQL generation
   - Custom API: More control over query optimization

   **Impact:** View/function creation strategy.

## Positive Patterns to Maintain

**1. Idempotent DDL throughout schema.sql**
```sql
-- Example from schema.sql:5-18
CREATE TABLE IF NOT EXISTS geographic_levels (...);
INSERT INTO geographic_levels (...) ON CONFLICT (level_code) DO NOTHING;
```
**Why this is excellent:** Enables safe re-application during deployments, supports GitOps workflows, prevents "table already exists" errors in CI/CD.

**2. Explicit CASCADE control on foreign keys**
```sql
-- schema.sql:68 - Prevent orphans
parent_psgc CHAR(10) REFERENCES locations(psgc_code) ON UPDATE CASCADE ON DELETE RESTRICT

-- schema.sql:86 - Clean up attributes
psgc_code CHAR(10) NOT NULL REFERENCES locations(psgc_code) ON DELETE CASCADE
```
**Why this is excellent:** Intentional behavior documented in DDL. ON DELETE RESTRICT for spine prevents accidental data loss. ON DELETE CASCADE for attributes ensures referential integrity without manual cleanup.

**3. Reference tables instead of raw enums**
```sql
-- schema.sql:5-61 - geographic_levels, city_class_types, income_brackets, urban_rural_tags
```
**Why this is excellent:** Human-readable values, easily extensible (just INSERT new row), supports joins for descriptions, enables constraint enforcement via FK.

**4. Separation of concerns in table design**
```sql
-- locations: core hierarchy
-- population_stats: time-series measurements
-- city_classifications: sparse attribute (143 cities only)
```
**Why this is excellent:** Avoids sparse columns, enables targeted indexing, simplifies queries (no NULL handling), clear semantics.

**5. Timestamp with time zone**
```sql
-- schema.sql:126
created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
```
**Why this is excellent:** Handles users in different timezones, preserves UTC offset, prevents ambiguity during DST transitions.

**6. Explicit column lists in COPY (deploy_to_db.py:31-55)**
```python
COPY_COLUMNS = {
    "locations": ["psgc_code", "name", "level_code", ...],
    ...
}
```
**Why this is excellent:** Resilient to column reordering, explicit about what's being loaded, avoids "column count mismatch" errors, documents ETL contract.

**7. Dependency-ordered table loading (deploy_to_db.py:128-134)**
```python
load_order = ["locations", "population_stats", "city_classifications", ...]
```
**Why this is excellent:** Respects foreign key constraints, prevents "violates foreign key constraint" errors, self-documenting load dependencies.

## Migration Path for Recommended Changes

### Phase 1: Critical Fixes (Immediate - Before Production Exposure)
**Timeline:** 1-2 days
**Downtime required:** None (can apply to existing database)

1. **Add critical indexes (Issue #1, #2, #3)**
```bash
# Create migration script: migrations/001_add_critical_indexes.sql
cat > migrations/001_add_critical_indexes.sql << 'EOF'
-- Critical: Foreign key index for population joins
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_population_stats_psgc
ON population_stats(psgc_code);

-- Critical: Composite index for parent+level queries
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_parent_level
ON locations(parent_psgc, level_code)
WHERE parent_psgc IS NOT NULL;

-- Critical: Level-based filtering
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_level
ON locations(level_code);

-- Partial indexes for common levels
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_level_prov
ON locations(level_code, name)
WHERE level_code = 'Prov';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_level_city
ON locations(level_code, name)
WHERE level_code IN ('City', 'Mun');

ANALYZE locations;
ANALYZE population_stats;
EOF

# Apply migration
source .venv/bin/activate
set -a && source .env && set +a
psql "$DATABASE_URL" -f migrations/001_add_critical_indexes.sql
```
**Validation:**
```sql
-- Verify indexes created
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename IN ('locations', 'population_stats')
ORDER BY tablename, indexname;

-- Test query performance improvement
EXPLAIN ANALYZE
SELECT l.name, ps.population
FROM population_stats ps
JOIN locations l ON l.psgc_code = ps.psgc_code
WHERE ps.reference_year = 2024 AND l.level_code = 'Prov'
ORDER BY ps.population DESC LIMIT 5;
-- Should show "Index Scan" not "Seq Scan"
```

2. **Add constraint validations (Issue #6)**
```bash
cat > migrations/002_add_constraints.sql << 'EOF'
-- PSGC code format validation
ALTER TABLE locations
ADD CONSTRAINT chk_psgc_code_format
CHECK (psgc_code ~ '^\d{10}$');

ALTER TABLE population_stats
ADD CONSTRAINT chk_psgc_code_format
CHECK (psgc_code ~ '^\d{10}$');

-- Prevent self-referencing parents
ALTER TABLE locations
ADD CONSTRAINT chk_no_self_parent
CHECK (parent_psgc IS NULL OR parent_psgc <> psgc_code);

-- Name non-empty
ALTER TABLE locations
ADD CONSTRAINT chk_name_not_empty
CHECK (TRIM(name) <> '');

-- Year ranges
ALTER TABLE population_stats
ADD CONSTRAINT chk_reference_year_range
CHECK (reference_year BETWEEN 1900 AND 2100);
EOF

psql "$DATABASE_URL" -f migrations/002_add_constraints.sql
```

3. **Fix deployment safety (Issue #4)**
```bash
# Update deploy_to_db.py per recommendations in "Operational Recommendations"
# Change TRUNCATE to DELETE, add transaction wrapping
# Test on staging database first
```

### Phase 2: Performance Improvements (1-2 weeks)
**Timeline:** Before exposing via API
**Downtime required:** None (CONCURRENT operations)

1. **Add covering indexes for common queries**
```bash
cat > migrations/003_covering_indexes.sql << 'EOF'
-- Covering index for hierarchy queries
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_parent_covering
ON locations(parent_psgc, level_code, name, psgc_code)
WHERE parent_psgc IS NOT NULL;

-- Covering index for population rankings
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_population_stats_covering
ON population_stats(reference_year, population DESC, psgc_code, source)
WHERE reference_year >= 2020;

ANALYZE locations;
ANALYZE population_stats;
EOF

psql "$DATABASE_URL" -f migrations/003_covering_indexes.sql
```

2. **Add name search support**
```bash
cat > migrations/004_name_search.sql << 'EOF'
-- B-tree for exact matches
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_name
ON locations(name);

-- Case-insensitive search
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_name_lower
ON locations(LOWER(name));

-- Trigram for fuzzy search (optional)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_name_trgm
ON locations USING gin (name gin_trgm_ops);

ANALYZE locations;
EOF

psql "$DATABASE_URL" -f migrations/004_name_search.sql
```

3. **Enable pg_stat_statements monitoring**
```bash
cat > migrations/005_monitoring.sql << 'EOF'
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Create monitoring views for easy dashboard integration
CREATE OR REPLACE VIEW slow_queries AS
SELECT
    substring(query, 1, 100) AS short_query,
    calls,
    total_exec_time,
    mean_exec_time,
    max_exec_time
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 20;

CREATE OR REPLACE VIEW table_stats AS
SELECT
    schemaname,
    tablename,
    n_live_tup,
    n_dead_tup,
    n_dead_tup::float / NULLIF(n_live_tup, 0) AS dead_ratio,
    last_vacuum,
    last_autovacuum
FROM pg_stat_user_tables
WHERE schemaname = 'public';
EOF

psql "$DATABASE_URL" -f migrations/005_monitoring.sql
```

### Phase 3: Future Enhancements (As Needed)
**Timeline:** When geometry data available or dataset exceeds 500k rows

1. **PostGIS geometry integration (when SHP files available)**
```bash
cat > migrations/006_geometry_load.sql << 'EOF'
-- Remove premature spatial index
DROP INDEX IF EXISTS idx_locations_geom;

-- Add geography column for accurate measurements
ALTER TABLE locations
ADD COLUMN IF NOT EXISTS geog GEOGRAPHY(MultiPolygon, 4326);

-- After loading geometries via SHP2PGSQL or similar:
-- UPDATE locations SET geog = geom::geography WHERE geom IS NOT NULL;

-- Create conditional spatial indexes
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_geom
ON locations USING GIST (geom)
WHERE geom IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_geog
ON locations USING GIST (geog)
WHERE geog IS NOT NULL;

-- Partial indexes by level
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_locations_geom_regions
ON locations USING GIST (geom)
WHERE level_code = 'Reg' AND geom IS NOT NULL;

ANALYZE locations;
EOF
```

2. **Partitioning (when population_stats exceeds 500k rows)**
```bash
# This requires data migration, plan carefully
cat > migrations/007_partition_population.sql << 'EOF'
-- 1. Rename existing table
ALTER TABLE population_stats RENAME TO population_stats_old;

-- 2. Create partitioned table
CREATE TABLE population_stats (
    population_id BIGSERIAL NOT NULL,
    psgc_code CHAR(10) NOT NULL REFERENCES locations(psgc_code) ON DELETE CASCADE,
    reference_year SMALLINT NOT NULL,
    population BIGINT NOT NULL CHECK (population >= 0),
    source TEXT NOT NULL,
    collected_at DATE DEFAULT CURRENT_DATE,
    PRIMARY KEY (psgc_code, reference_year, source)
) PARTITION BY RANGE (reference_year);

-- 3. Create partitions
CREATE TABLE population_stats_2020s PARTITION OF population_stats
    FOR VALUES FROM (2020) TO (2030);

CREATE TABLE population_stats_2030s PARTITION OF population_stats
    FOR VALUES FROM (2030) TO (2040);

-- 4. Migrate data
INSERT INTO population_stats SELECT * FROM population_stats_old;

-- 5. Recreate indexes
CREATE INDEX idx_population_stats_psgc ON population_stats(psgc_code);
CREATE INDEX idx_population_stats_year ON population_stats(reference_year);

-- 6. Verify and drop old table
-- SELECT COUNT(*) FROM population_stats;
-- SELECT COUNT(*) FROM population_stats_old;
-- DROP TABLE population_stats_old;
EOF
```

3. **Blue-green deployment (when exposing to production users)**
```bash
# Update deploy_to_db.py with blue-green logic from "Operational Recommendations"
# Test thoroughly on staging environment
# Document rollback procedure
```

### Migration Validation Checklist
After each phase:
- [ ] Run ANALYZE on affected tables
- [ ] Check query plans for index usage: `EXPLAIN ANALYZE <query>`
- [ ] Verify constraint enforcement: Attempt invalid INSERT
- [ ] Monitor pg_stat_user_indexes for index scans
- [ ] Check table sizes: `SELECT pg_size_pretty(pg_total_relation_size('locations'))`
- [ ] Test ETL reload process
- [ ] Backup database before major changes
- [ ] Document migration in CHANGELOG.md

## Production Readiness Assessment

**Overall Score:** 6.5/10

**Breakdown:**
- **Schema Design:** 9/10 - Excellent normalization, clear semantics, minor indexing gaps
- **Data Integrity:** 7/10 - Good FK usage, missing format constraints
- **Performance:** 4/10 - Critical missing indexes, no query monitoring
- **Operational Safety:** 5/10 - Truncate-and-reload breaks concurrent queries
- **Scalability:** 7/10 - Design scales to 10M rows, needs partitioning plan
- **Monitoring:** 3/10 - No pg_stat_statements, no alerting
- **Documentation:** 9/10 - Excellent code documentation, clear architecture
- **PostGIS Readiness:** 5/10 - Schema ready, but premature index creation

**Gaps to Production:**

1. **Critical (Blocks production launch):**
   - Missing indexes on population_stats.psgc_code (JOIN performance)
   - Missing composite index on locations(parent_psgc, level_code)
   - Truncate-and-reload causes query failures during deployment
   - No pg_stat_statements for performance monitoring

2. **High (Should fix before public API exposure):**
   - Missing constraints on psgc_code format
   - No index on locations.level_code
   - Premature spatial index on NULL column
   - No connection pooling plan for concurrent users

3. **Medium (Fix within 3 months of launch):**
   - No name search indexes
   - Missing query performance monitoring dashboard
   - No blue-green deployment for zero downtime
   - Geography column for accurate distance calculations

4. **Low (Future improvements):**
   - Table partitioning for long-term data retention
   - Materialized views for common aggregations
   - Automated index analysis
   - Point-in-time recovery testing

**Performance Baseline (Estimated):**

**Current state (without recommended indexes):**
- Hierarchical query (children of parent): 50-200ms (sequential scan)
- Top 5 provinces by population: 100-300ms (hash join with seq scans)
- Level-filtered query (all cities): 80-250ms (seq scan)
- Name search: 150-500ms (seq scan)

**After Phase 1 fixes (critical indexes):**
- Hierarchical query: 5-15ms (index scan)
- Top 5 provinces by population: 10-30ms (nested loop with index)
- Level-filtered query: 8-20ms (index scan)
- Name search: 15-40ms (index scan)

**After Phase 2 improvements (covering indexes):**
- Hierarchical query: 2-8ms (index-only scan)
- Top 5 provinces by population: 5-12ms (index-only scan)
- Level-filtered query: 3-10ms (index-only scan)
- Name search (fuzzy): 20-60ms (GIN index scan)

**Scalability Limits:**

**Current schema can handle:**
- 10M location rows (recursive CTEs acceptable to depth 10)
- 50M population measurements (with partitioning)
- 1,000 concurrent read queries (with connection pooling)
- 10 GB database size (Neon storage tier)

**Performance degradation expected at:**
- 100M rows in locations (recursive CTEs > 500ms)
- 500M rows in population_stats without partitioning (VACUUM > 1 hour)
- 5,000 concurrent connections without PgBouncer (connection exhaustion)
- 100 GB database size (Neon autoscaling limits)

**Immediate Action Items (Priority Order):**
1. Apply migrations/001_add_critical_indexes.sql (30 minutes, no downtime)
2. Add constraints (migrations/002_add_constraints.sql) (15 minutes, no downtime)
3. Fix deploy_to_db.py truncate-and-reload (2 hours development + testing)
4. Enable pg_stat_statements monitoring (5 minutes)
5. Create performance dashboard (1 day, optional)

**After Phase 1 completion, reassess score: Expected 8.5/10**

---

**Final Recommendation:**
The schema foundation is strong, but critical indexing and deployment safety issues must be addressed before production use. Implement Phase 1 migrations immediately (estimated 4 hours total). The database is suitable for internal use today and will be production-ready for public API exposure after Phase 2 (estimated 1 week). Long-term scalability is sound with proper indexing and monitoring in place.
