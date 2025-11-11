## Problem

The PSA PSGC workbook is publication-oriented: merged headers, scattered notes, and sparse attributes make it hard to ask simple questions like “Which provinces have the highest population?” or to power map visualizations and APIs. Every analysis required manual cleaning, and there was no centralized, queryable store.

## Goal

Build a reliable, hosted datastore that ingests each PSGC release, preserves the geographic hierarchy (regions → barangays), includes up-to-date 2024 population figures, and exposes the data through SQL/API access so population rankings and mapping layers can be answered programmatically.

## What We Did

1. **Explored the Excel source** with `analyze_psgc.py` to understand sheet structure, sparsity, and key metrics.
2. **Designed a normalized PostgreSQL/PostGIS schema** (`schema.sql`) centered on a `locations` spine with supporting tables for population stats, city/income classes, and urban/rural tags.
3. **Built an ETL/export pipeline** (`etl_psgc.py`) that cleans the PSA workbook, infers parent PSGC codes, and emits CSVs aligned with the schema.
4. **Automated deployment** via `deploy_to_db.py`, which runs the ETL, reapplies the schema, and streams the CSVs into the Neon PostgreSQL instance referenced in `.env`.
5. Loaded the Neon database (`philippine_standard_geographic_code`) so it now holds 43,769 locations, 43,768 population rows, 149 city classifications, 1,724 income classifications, and 42,011 settlement tags (as of PSGC Q3 2025 / 2024 POPCEN).

## Current Status (Where We Are Now)

- Neon DB is seeded: querying it shows the full PSGC hierarchy with 2024 populations (e.g., Cavite 4.57M, Davao City 1.85M, etc.).
- `deploy_to_db.py` can be re-run after each PSA release to refresh everything end-to-end (`source .venv/bin/activate && set -a && source .env && set +a && python deploy_to_db.py --workbook ...`).
- `data_exports/` always reflects the latest ETL run, so CSVs are available for downstream tooling or archival.
- Documentation (`DATABASE_PLAN.md`, this file) captures the architecture and usage steps, ready for CI automation or API exposure (PostgREST/Hasura/Supabase).

## How to Use the Database / Dataset

1. **Local prerequisites**
   - `python3 -m venv .venv && source .venv/bin/activate`
   - `pip install -r requirements` (currently `pandas`, `openpyxl`, `psycopg[binary]`)
   - Copy the Neon connection string into `.env` as `DATABASE_URL="postgresql://..."`

2. **Regenerate & load (one command)**
   ```bash
   source .venv/bin/activate
   set -a && source .env && set +a
   python deploy_to_db.py --workbook PSGC-3Q-2025-Publication-Datafile.xlsx
   ```
   This:
   - runs the ETL, filling `data_exports/*.csv`;
   - reapplies `schema.sql` (idempotent, seeds reference tables);
   - truncates and reloads Neon tables via streaming COPY.

3. **Querying Neon**
   ```bash
   psql "$DATABASE_URL" -c "
     SELECT l.name, ps.population
     FROM population_stats ps
     JOIN locations l ON l.psgc_code = ps.psgc_code
     WHERE ps.reference_year = 2024 AND l.level_code = 'Prov'
     ORDER BY ps.population DESC LIMIT 5;"
   ```
   Any SQL client (psql, DBeaver, Hasura, PostgREST) can connect using the same `DATABASE_URL`, so you can build dashboards, APIs, or GIS joins.

4. **Use the CSVs directly** if you simply need flat files (`data_exports/locations.csv`, etc.)—they mirror the database tables.

Next steps could include: wiring `deploy_to_db.py` into CI for scheduled refreshes, adding PostGIS geometries, or standing up PostgREST/Hasura to expose REST/GraphQL APIs. For now, the Neon database is ready for analytical queries and map-driven use cases. 
