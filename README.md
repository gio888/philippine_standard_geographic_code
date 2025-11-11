## Philippine Standard Geographic Code (PSGC) Data Pipeline

This project converts PSA’s PSGC publication workbook into a normalized PostgreSQL/PostGIS dataset hosted on Neon, enabling population analytics (regions → barangays) and future map visualizations/APIs.

### Problem
The official Excel release is formatting-heavy (merged headers, notes, sparse attributes), so answering “Which province has the largest population?” requires manual cleanup each time. There was no centralized, queryable store for PSGC + population data.

### Goal
Provide a reliable, hosted datastore that captures the PSGC hierarchy, includes 2024 POPCEN counts, and can be refreshed every PSA release so downstream tools can query it (SQL, REST/GraphQL, GIS).

### Solution Overview
1. **Exploration**: `analyze_psgc.py` inspects the workbook and surfaces key stats.
2. **Schema**: `schema.sql` defines a normalized structure (`locations`, `population_stats`, city/income class tables, settlement tags) plus reference enums (levels, income brackets, urban/rural codes). PostGIS hooks are in place for future geometries.
3. **ETL**: `etl_psgc.py` cleans the PSGC sheet, infers parent PSGC codes, fills CSV exports (`data_exports/*.csv`).
4. **Deployment**: `deploy_to_db.py` runs the ETL, reapplies the schema, and streams CSVs into Neon via COPY. It reads `DATABASE_URL` from `.env`.

### Current State
- Neon database `philippine_standard_geographic_code` contains 43,769 location rows, 43,768 population rows (2024 POPCEN), 149 city classifications, 1,724 income classifications, and 42,011 settlement tags (Q3 2025 release).
- One command refreshes everything: `python deploy_to_db.py --workbook PSGC-3Q-2025-Publication-Datafile.xlsx` (after sourcing `.venv` + `.env`).
- Documentation: `DATABASE_PLAN.md` (architecture/steps), `PROJECT_STATUS.md` (status + usage notes).

### Usage
1. **Setup**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install pandas openpyxl psycopg[binary]
   echo 'DATABASE_URL="postgresql://...neon.../philippine_standard_geographic_code?sslmode=require&channel_binding=require"' > .env
   ```

2. **Refresh data (preferred)**
   ```bash
   source .venv/bin/activate
   set -a && source .env && set +a
   python deploy_to_db.py --workbook PSGC-3Q-2025-Publication-Datafile.xlsx
   ```
   This regenerates `data_exports/*.csv`, reapplies the schema, and reloads Neon.

3. **Manual CSV-only run**
   ```bash
   python etl_psgc.py --workbook PSGC-3Q-2025-Publication-Datafile.xlsx --reference-year 2024 --source-label "2024 POPCEN (PSA)"
   ```
   CSVs remain in `data_exports/` for other tooling.

4. **Query Neon**
   ```bash
   psql "$DATABASE_URL" -c "
     SELECT l.name, ps.population
     FROM population_stats ps
     JOIN locations l ON l.psgc_code = ps.psgc_code
     WHERE ps.reference_year = 2024 AND l.level_code = 'Prov'
     ORDER BY ps.population DESC LIMIT 5;"
   ```
   Any SQL client (psql, DBeaver, Hasura/PostgREST) can use the same `DATABASE_URL`.

5. **Answering questions**
   - Top 10 non-NCR cities: filter `SUBSTRING(l.psgc_code, 1, 2) <> '13'`.
   - Largest provinces: `level_code = 'Prov'`.
   - Barangay counts by province: aggregate `locations` filtered by parent.

### Next Steps
- Automate deploy via CI (GitHub Actions) for each new PSA release.
- Attach PostGIS geometries and expose a REST/GraphQL API (PostgREST/Hasura/Supabase).
- Build dashboards or map layers using the normalized tables or `data_exports` CSVs.

See `PROJECT_STATUS.md` and `DATABASE_PLAN.md` for deeper context and step-by-step instructions.
