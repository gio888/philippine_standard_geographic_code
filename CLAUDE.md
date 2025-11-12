# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Philippine Standard Geographic Code (PSGC) data pipeline that converts PSA's quarterly Excel publication into a normalized PostgreSQL/PostGIS database hosted on Neon. The system enables SQL-based population analytics from regions down to barangays (43,769 locations total) and supports future GIS visualizations.

The codebase consists of three main Python scripts that form a complete ETL pipeline:
1. `analyze_psgc.py` - Initial exploration/analysis tool
2. `etl_psgc.py` - Core ETL logic that transforms Excel to normalized CSVs
3. `deploy_to_db.py` - Orchestrates ETL + schema migration + database loading

## Commands

### Environment Setup
```bash
# Initial setup
python3 -m venv .venv
source .venv/bin/activate
pip install pandas openpyxl psycopg[binary]

# Create .env file with Neon connection string
echo 'DATABASE_URL="postgresql://...neon.../philippine_standard_geographic_code?sslmode=require&channel_binding=require"' > .env
```

### Primary Workflow
```bash
# Full refresh (recommended): ETL + schema + database load
source .venv/bin/activate
set -a && source .env && set +a
python deploy_to_db.py --workbook PSGC-3Q-2025-Publication-Datafile.xlsx
```

# Python environment rules (IMPORTANT)
- Always use a project-local virtual environment `.venv`.
- If `.venv` doesn't exist: create it with `python3 -m venv .venv`.
- Activate it before any Python command: `source .venv/bin/activate`.
- Never run `pip install` without the venv active.
- Prefer `uv` if available: `uv sync`, `uv run`, `uv add <pkg>`.

### Individual Operations
```bash
# Generate CSVs only (no database interaction)
python etl_psgc.py --workbook PSGC-3Q-2025-Publication-Datafile.xlsx --reference-year 2024 --source-label "2024 POPCEN (PSA)"

# Explore workbook structure
python analyze_psgc.py

# Apply schema directly (requires DATABASE_URL env)
psql "$DATABASE_URL" -f schema.sql

# Manual CSV loading (after running etl_psgc.py)
psql "$DATABASE_URL" -c "\copy locations FROM 'data_exports/locations.csv' CSV HEADER"
psql "$DATABASE_URL" -c "\copy population_stats FROM 'data_exports/population_stats.csv' CSV HEADER"
psql "$DATABASE_URL" -c "\copy city_classifications FROM 'data_exports/city_classifications.csv' CSV HEADER"
psql "$DATABASE_URL" -c "\copy income_classifications FROM 'data_exports/income_classifications.csv' CSV HEADER"
psql "$DATABASE_URL" -c "\copy settlement_tags FROM 'data_exports/settlement_tags.csv' CSV HEADER"
```

### Querying Examples
```bash
# Top 5 provinces by population
psql "$DATABASE_URL" -c "
  SELECT l.name, ps.population
  FROM population_stats ps
  JOIN locations l ON l.psgc_code = ps.psgc_code
  WHERE ps.reference_year = 2024 AND l.level_code = 'Prov'
  ORDER BY ps.population DESC LIMIT 5;"

# Barangay count per province
psql "$DATABASE_URL" -c "
  SELECT p.name AS province, COUNT(b.psgc_code) AS barangay_count
  FROM locations b
  JOIN locations p ON p.psgc_code = b.parent_psgc
  WHERE b.level_code = 'Bgy' AND p.level_code = 'Prov'
  GROUP BY p.name
  ORDER BY barangay_count DESC;"
```

## Architecture

### Data Flow
The pipeline follows a three-stage process:

1. **Extract** (`etl_psgc.py:load_psgc`): Reads the "PSGC" sheet from PSA's Excel workbook, normalizes PSGC codes to 10-digit zero-padded strings, and cleans column names.

2. **Transform** (`etl_psgc.py:export_tables`):
   - Infers parent-child relationships using geographic hierarchy rules (regions → provinces → cities/municipalities → sub-municipalities → barangays)
   - Splits sparse multi-attribute rows into normalized tables
   - Validates referential integrity (parent codes must exist before children)

3. **Load** (`deploy_to_db.py:copy_csv`): Streams CSV data to Neon PostgreSQL via psycopg `COPY` protocol (1 MB chunks), truncating tables before each load to ensure idempotency.

### Parent Inference Logic (`etl_psgc.py:26-49`)
PSGC codes follow a positional structure: `RRPPCCSSBB` where:
- RR = Region (positions 0-1)
- PP = Province (positions 2-3)
- CC = City/Municipality (positions 4-5)
- SS = Sub-municipality (positions 6-7)
- BB = Barangay (positions 8-9)

The `candidate_parents` function generates potential parent codes by masking lower digits with zeros (e.g., barangay `1234567890` → candidates `1234560000`, `1234000000`, `1200000000`). The first matching candidate from the valid codes set becomes the parent.

### Database Schema (`schema.sql`)
Core design principles:
- **Spine table**: `locations` holds all 43,769 PSGC entries with self-referencing foreign key (`parent_psgc`) for hierarchy
- **Attribute tables**: Separate tables for population stats, city classifications, income brackets, and urban/rural tags to avoid sparse columns
- **Reference tables**: Enum-like lookup tables (`geographic_levels`, `city_class_types`, `income_brackets`, `urban_rural_tags`) seeded with INSERT...ON CONFLICT
- **PostGIS ready**: `geom GEOMETRY(MultiPolygon, 4326)` column exists but unpopulated; spatial index pre-created
- **Idempotency**: All CREATE TABLE use `IF NOT EXISTS`; ETL truncates before load

### File Organization
```
├── PSGC-3Q-2025-Publication-Datafile.xlsx  # Source data (quarterly PSA release)
├── analyze_psgc.py                          # Exploration tool (inspect sheets, stats)
├── etl_psgc.py                              # ETL engine (Excel → CSVs)
├── deploy_to_db.py                          # Orchestrator (ETL + schema + load)
├── schema.sql                               # Database DDL (idempotent)
├── data_exports/                            # Generated CSVs (5 normalized tables)
│   ├── locations.csv
│   ├── population_stats.csv
│   ├── city_classifications.csv
│   ├── income_classifications.csv
│   └── settlement_tags.csv
├── DATABASE_PLAN.md                         # Architecture decisions & alternatives
├── PROJECT_STATUS.md                        # Current state & usage guide
└── README.md                                # User-facing documentation
```

## Key Constraints & Behaviors

### ETL Logic
- **Code normalization**: All PSGC codes are zero-padded to exactly 10 digits. Non-numeric codes or empty cells are treated as `None`.
- **Parent inference order**: The system tries more specific parents first (e.g., for barangays: sub-municipality → city/municipality → province → region).
- **Level ranking**: When sorting locations, the order is: Reg(0), Prov(1), City/Mun(2), SubMun(3), Bgy(4), Other(5).
- **Population rounding**: The `population_2024` column is rounded to integers before CSV export.

### Database Constraints
- **Foreign key enforcement**: Children cannot exist without valid parents. The `ON DELETE RESTRICT` on `parent_psgc` prevents orphaning.
- **Cascade deletes**: Attribute tables use `ON DELETE CASCADE`, so deleting a location removes its population stats and classifications.
- **Unique constraints**: `(psgc_code, reference_year, source)` in `population_stats` prevents duplicate population entries for the same year/source.
- **Check constraints**: Population values must be non-negative (`CHECK (population >= 0)`).

### Deploy Script Behavior (`deploy_to_db.py`)
- **Atomicity**: Schema application uses `autocommit=True` to avoid transaction wrapping of DDL.
- **Load order**: Tables are loaded in dependency order: `locations` first (no dependencies), then tables with foreign keys to locations.
- **Truncation**: Each table is truncated before loading to ensure clean state. This is safe because the ETL regenerates all CSVs.
- **Error handling**: Missing CSVs raise `FileNotFoundError`. Missing `DATABASE_URL` raises `SystemExit`.

## Common Patterns

### Adding New PSGC Attributes
1. Add column to `schema.sql` in the appropriate table (or create new attribute table if multi-valued)
2. Update `etl_psgc.py:export_tables` to extract and export the new column to CSV
3. Add column name to `COPY_COLUMNS` dict in `deploy_to_db.py` if using explicit column list
4. Run `deploy_to_db.py` to regenerate CSVs and reload database

### Refreshing for New PSA Release
```bash
# 1. Download new workbook from PSA (adjust filename as needed)
# 2. Run full deploy
python deploy_to_db.py --workbook PSGC-4Q-2025-Publication-Datafile.xlsx --reference-year 2024
```

### Querying Hierarchy
Use recursive CTEs or self-joins on `parent_psgc`:
```sql
-- All locations under NCR (region code 1300000000)
WITH RECURSIVE hierarchy AS (
  SELECT psgc_code, name, level_code, parent_psgc
  FROM locations
  WHERE psgc_code = '1300000000'
  UNION ALL
  SELECT l.psgc_code, l.name, l.level_code, l.parent_psgc
  FROM locations l
  JOIN hierarchy h ON l.parent_psgc = h.psgc_code
)
SELECT * FROM hierarchy;
```

### Filtering by Geographic Level
Level codes are stored in `geographic_levels` reference table:
- `'Reg'` - Region
- `'Prov'` - Province
- `'City'` - City
- `'Mun'` - Municipality
- `'SubMun'` - Sub-municipality/district
- `'Bgy'` - Barangay
- `'Other'` - Special PSGC aggregates

Example: "Top 10 most populous cities excluding NCR"
```sql
SELECT l.name, ps.population
FROM population_stats ps
JOIN locations l ON l.psgc_code = ps.psgc_code
WHERE l.level_code = 'City'
  AND SUBSTRING(l.psgc_code, 1, 2) <> '13'
  AND ps.reference_year = 2024
ORDER BY ps.population DESC
LIMIT 10;
```

## Notes

- The project currently uses Q3 2025 PSGC publication with 2024 POPCEN (census) population data.
- PostGIS geometries are not yet populated but the schema is ready (`geom` column + spatial index exist).
- The database is hosted on Neon (serverless Postgres) and requires `sslmode=require&channel_binding=require` in connection string.
- Future enhancements may include CI/CD automation, REST/GraphQL API layer (PostgREST/Hasura), and boundary shape files (SHP/GeoJSON).
