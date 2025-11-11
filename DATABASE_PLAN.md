## Goal

Provide a reliable, queryable PSGC datastore that powers population rankings (regions → barangays) and supports map visualizations via APIs.

## Situation

- **Raw data available**: quarterly PSA “PSGC Publication Datafile” Excel workbook (`PSGC-3Q-2025-Publication-Datafile.xlsx`).
- **Characteristics**: contains 7 sheets; main `PSGC` sheet lists 43,769 geographic units with PSGC codes, names, hierarchy levels, and 2024 population. Summary sheets mix text blocks, header rows, and numeric tables.
- **Problems**: heavy formatting, merged headers, inline notes, and sparse attribute columns make it hard to query directly; no spatial geometries or historical versioning, so every analysis needs manual cleanup first.

## Recommended Database

**PostgreSQL + PostGIS (managed host such as AWS RDS, Supabase, or Azure Flexible Server).**
- Mature relational engine with strong integrity features for hierarchical foreign keys.
- Flexible column types (arrays/JSON) for optional metadata without bloating the core table.
- PostGIS adds native spatial operations, making choropleths and containment queries straightforward.
- Large ecosystem of drop-in API layers (PostgREST, Hasura, Supabase) to expose REST/GraphQL endpoints without bespoke services.

## Alternative Options Considered

1. **MySQL/Aurora MySQL**
   - Pros: familiar, cost-effective, wide tooling.
   - Cons: weaker GIS capabilities and JSON ergonomics; would require external GIS service for colored maps. Rejected to avoid split stack.
2. **BigQuery or Snowflake**
   - Pros: serverless scale, straightforward ingestion of large CSVs.
   - Cons: less suited for incremental OLTP-style updates, costs tied to query size, spatial support requires extra configuration/extensions. Overkill for modest PSGC datasets.
3. **NoSQL (MongoDB, Firestore)**
   - Pros: flexible schema.
   - Cons: enforcing hierarchy constraints, performing joins, and running aggregate rankings would be cumbersome; spatial queries need separate services.

**Chosen** PostgreSQL + PostGIS because it simultaneously satisfies integrity, spatial analytics, and API friendliness without extra components.

## Plan (with Justification)

1. **Design canonical schema**
   - *Action*: Define `locations`, `population_stats`, attribute lookup tables (city class, income class, urban/rural), and reference enums.
   - *Justification*: Normalization prevents duplication, enforces parent-child relationships via foreign keys, and keeps optional attributes modular.

2. **Enable PostGIS & store geometries**
   - *Action*: Install PostGIS, add `geom` columns where boundary data exists, and track SRID metadata.
   - *Justification*: Directly supports colored maps and geographic containment queries without leaving the DB layer.

3. **Build ingestion pipeline**
   - *Action*: Stage raw Excel sheets, clean them (strip headers, normalize codes), and load into the normalized tables using reproducible scripts.
   - *Justification*: Ensures future PSGC releases can be processed automatically with audit-friendly transformations.

4. **Provision managed hosting & API access**
   - *Action*: Deploy PostgreSQL on a managed service, layer PostgREST/Hasura/Supabase for REST/GraphQL endpoints, configure read-only roles.
   - *Justification*: Quickly exposes the dataset to apps/dashboards without writing custom backend code.

5. **Automate refreshes**
   - *Action*: Wire CI/CD (GitHub Actions) to run the ETL on new PSA releases, add validation tests (row counts, missing parents, code uniqueness), and push to the cloud DB.
   - *Justification*: Keeps data current while preventing regressions or hierarchy breaks.

6. **Document & onboard**
   - *Action*: Publish a data dictionary, ERD, API usage guide, and troubleshooting notes.
   - *Justification*: Captures institutional knowledge, accelerates consumer adoption, and clarifies why PostgreSQL was chosen over alternatives.

## Execution Guide

1. **Prepare local tooling**
   - `python3 -m venv .venv && source .venv/bin/activate`
   - `pip install pandas openpyxl psycopg[binary]` (psycopg optional if loading via Python instead of `psql`).

2. **Generate normalized CSVs / deploy**
   - Quick path: `python etl_psgc.py --workbook PSGC-3Q-2025-Publication-Datafile.xlsx --reference-year 2024 --source-label "2024 POPCEN (PSA)"` (fills `data_exports/*.csv` only).
   - Full automation (recommended): 
     ```bash
     source .venv/bin/activate
     set -a && source .env && set +a
     python deploy_to_db.py --workbook PSGC-3Q-2025-Publication-Datafile.xlsx
     ```
     This runs ETL, reapplies `schema.sql`, and reloads Neon using `DATABASE_URL`.

3. **Provision PostgreSQL**
   - Create a managed Postgres instance (e.g., AWS RDS / Supabase). Ensure PostGIS is available/enabled.

4. **Apply schema (manual option)**
   - `set -a && source .env && set +a`
   - `psql "$DATABASE_URL" -f schema.sql` (idempotent; seeds reference tables). *Skip if using `deploy_to_db.py`, which already executes this step.*

5. **Load data manually (only if you skipped deploy script)**
   - `psql "$DATABASE_URL" -c "\copy locations FROM 'data_exports/locations.csv' CSV HEADER"`
   - `psql "$DATABASE_URL" -c "\copy population_stats FROM 'data_exports/population_stats.csv' CSV HEADER"`
   - `psql "$DATABASE_URL" -c "\copy city_classifications FROM 'data_exports/city_classifications.csv' CSV HEADER"`
   - `psql "$DATABASE_URL" -c "\copy income_classifications FROM 'data_exports/income_classifications.csv' CSV HEADER"`
   - `psql "$DATABASE_URL" -c "\copy settlement_tags FROM 'data_exports/settlement_tags.csv' CSV HEADER"`

6. **Expose APIs**
   - Install and configure PostgREST/Hasura/Supabase to expose read-only REST/GraphQL endpoints.
   - Define row-level policies if the database will be publicly accessible.

7. **Automate refresh**
   - Script GitHub Action (or similar) that downloads the new PSA workbook, runs `deploy_to_db.py` (or separate ETL + COPY steps), validates row counts, and uploads via `psql` or direct COPY API to the managed database.
