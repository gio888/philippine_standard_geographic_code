# DevOps & Infrastructure Review - PSGC Deployment
**Date:** 2025-11-12 11:10
**Reviewer:** Backend Infrastructure Specialist
**Scope:** deploy_to_db.py, deployment patterns, operational excellence

## Executive Summary
The PSGC deployment system demonstrates functional ETL-to-database automation suitable for development environments, but contains **critical operational gaps that prevent safe production deployment**. The truncate-and-reload pattern with autocommit creates concurrency hazards, no transaction boundaries enable partial failures leaving inconsistent state, and complete absence of logging/monitoring creates operational blind spots. With 8-10 weeks of infrastructure hardening (error handling, blue-green deployment, CI/CD automation, monitoring), this system can achieve production readiness.

## Strengths
Observed operational best practices:

- **Idempotent schema design** (schema.sql): All DDL uses `IF NOT EXISTS` and `ON CONFLICT DO NOTHING`, enabling safe re-application without "already exists" errors (deploy_to_db.py:21-27)
- **Streaming COPY protocol** (deploy_to_db.py:69-76): 1MB chunked uploads avoid memory exhaustion, efficient for 40k+ row datasets
- **Explicit column ordering** (deploy_to_db.py:31-55): COPY_COLUMNS dictionary prevents column mismatch errors if schema evolves
- **Dependency-ordered loading** (deploy_to_db.py:128-134): Respects foreign key constraints (locations → attribute tables)
- **Clean separation of ETL and deployment** (etl_psgc.py vs deploy_to_db.py): Enables standalone CSV generation for testing
- **Environment-based configuration** (.env file): Keeps credentials out of code
- **Single-command deployment** (deploy_to_db.py): Orchestrates full pipeline (ETL → schema → load) in one operation

## Critical Issues

### Issue 1: No Transaction Boundaries - Partial Failure Risk
- **Location:** deploy_to_db.py:62-77 (copy_csv function)
- **Impact:** **CRITICAL**
- **Category:** Safety
- **Description:** Each table is loaded with `autocommit=True`, meaning TRUNCATE and COPY operations commit immediately. If deployment fails mid-table (network timeout, disk full, constraint violation), the database is left in inconsistent state with some tables truncated, some partially loaded, and no way to rollback.
- **Current Behavior:**
```python
# deploy_to_db.py:62-77
def copy_csv(conninfo: str, table: str, csv_path: Path) -> None:
    # ...
    with psycopg.connect(conninfo, autocommit=True) as conn:  # PROBLEM: autocommit
        with conn.cursor() as cur, csv_path.open("r", encoding="utf-8") as fh:
            cur.execute(f"TRUNCATE TABLE {table} CASCADE")  # Immediate commit
            # ... COPY operation
            # If this fails, table is empty with no rollback
```
- **Recommended Fix:**
```python
def copy_csv(conninfo: str, table: str, csv_path: Path) -> None:
    """Load CSV into table within a transaction for atomic operation."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found for {table}: {csv_path}")

    logger.info(f"Loading {table} from {csv_path}...")

    # Use transaction (autocommit=False is default)
    with psycopg.connect(conninfo) as conn:
        try:
            with conn.cursor() as cur, csv_path.open("r", encoding="utf-8") as fh:
                # All operations in single transaction
                cur.execute(f"TRUNCATE TABLE {table} CASCADE")

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

                # Explicit commit only on success
                conn.commit()
                logger.info(f"{table} loaded successfully ({cur.rowcount} rows)")

        except Exception as e:
            conn.rollback()  # Rollback on any error
            logger.error(f"Failed to load {table}: {e}")
            raise
```
- **Rationale:** Wrapping TRUNCATE + COPY in a transaction ensures atomicity. If COPY fails, TRUNCATE rolls back, leaving original data intact. This prevents the "empty table" failure scenario.

### Issue 2: Truncate-and-Reload Blocks Concurrent Queries
- **Location:** deploy_to_db.py:64 (`TRUNCATE TABLE {table} CASCADE`)
- **Impact:** **CRITICAL**
- **Category:** Safety
- **Description:** `TRUNCATE` acquires `ACCESS EXCLUSIVE` lock on the table, blocking all concurrent readers/writers. On Neon (serverless Postgres), active queries will fail with "could not obtain lock" or "relation does not exist" errors. The `CASCADE` modifier amplifies this by propagating locks to child tables (population_stats, city_classifications, etc.), creating a ~30-60 second outage window during deployment.
- **Failure Scenario:**
```
Timeline:
00:00 - User query: SELECT * FROM locations WHERE level_code = 'Prov'
00:01 - Deployment starts: TRUNCATE TABLE locations CASCADE
00:01 - User query blocked waiting for lock
00:02 - COPY begins (locations table empty, lock still held)
00:30 - COPY completes, lock released
00:30 - User query resumes but sees partial/empty data if not transacted
```
- **Recommended Fix (Level 1: Immediate, simpler):**
```python
def copy_csv(conninfo: str, table: str, csv_path: Path) -> None:
    """Load CSV using DELETE instead of TRUNCATE for better concurrency."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found for {table}: {csv_path}")

    logger.info(f"Loading {table} from {csv_path}...")

    with psycopg.connect(conninfo) as conn:
        try:
            with conn.cursor() as cur, csv_path.open("r", encoding="utf-8") as fh:
                # DELETE instead of TRUNCATE (row-level locks, allows concurrent reads via MVCC)
                cur.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))

                columns = COPY_COLUMNS.get(table)
                if columns:
                    column_list = sql.SQL(", ").join(map(sql.Identifier, columns))
                    copy_sql = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(
                        sql.Identifier(table),
                        column_list
                    )
                else:
                    copy_sql = sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv, HEADER true)").format(
                        sql.Identifier(table)
                    )

                with cur.copy(copy_sql) as copy:
                    while True:
                        chunk = fh.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        copy.write(chunk)

                conn.commit()

                # VACUUM after DELETE to reclaim space
                conn.autocommit = True
                cur.execute(sql.SQL("VACUUM ANALYZE {}").format(sql.Identifier(table)))

            logger.info(f"{table} loaded successfully")

        except Exception as e:
            logger.error(f"Failed to load {table}: {e}")
            raise
```
- **Rationale:** `DELETE` uses MVCC (Multi-Version Concurrency Control), allowing concurrent reads to see old data until commit. Slower than TRUNCATE but safe for production with active queries. Requires VACUUM afterward to reclaim space.

### Issue 3: No Deployment Rollback Mechanism
- **Location:** deploy_to_db.py:117-140 (main function)
- **Impact:** **CRITICAL**
- **Category:** Safety
- **Description:** If deployment fails after loading 3 of 5 tables, there's no rollback mechanism. The database is left in mixed state (new data in locations/population_stats, old data in remaining tables). Manual recovery requires re-running deployment or restoring from backup, but no backup automation exists.
- **Current Behavior:**
```python
# deploy_to_db.py:128-138
for table in load_order:
    csv_path = output_dir / f"{table}.csv"
    copy_csv(conninfo, table, csv_path)  # Each table commits independently
    # If this fails, previous tables already committed, no rollback
```
- **Recommended Fix:**
```python
def deploy_all_tables(conninfo: str, load_order: list[str], output_dir: Path) -> None:
    """Deploy all tables in a single transaction for atomic all-or-nothing behavior."""
    logger.info("Starting database deployment...")

    # Pre-flight validation
    for table in load_order:
        csv_path = output_dir / f"{table}.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {csv_path}")
        logger.info(f"Validated CSV exists: {csv_path}")

    # Single transaction for entire deployment
    with psycopg.connect(conninfo) as conn:
        try:
            with conn.cursor() as cur:
                # Delete from all tables first (in dependency order)
                for table in load_order:
                    cur.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
                    logger.info(f"Truncated {table}")

                # Load all tables
                rows_loaded = {}
                for table in load_order:
                    csv_path = output_dir / f"{table}.csv"
                    with csv_path.open("r", encoding="utf-8") as fh:
                        columns = COPY_COLUMNS.get(table)
                        if columns:
                            column_list = sql.SQL(", ").join(map(sql.Identifier, columns))
                            copy_sql = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(
                                sql.Identifier(table),
                                column_list
                            )
                        else:
                            copy_sql = sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv, HEADER true)").format(
                                sql.Identifier(table)
                            )

                        with cur.copy(copy_sql) as copy:
                            while chunk := fh.read(CHUNK_SIZE):
                                copy.write(chunk)

                        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
                        count = cur.fetchone()[0]
                        rows_loaded[table] = count
                        logger.info(f"Loaded {table}: {count} rows")

                # Validation before commit
                if rows_loaded['locations'] == 0:
                    raise ValueError("No locations loaded - aborting deployment")

                # Commit only if all tables loaded successfully
                conn.commit()
                logger.info(f"Deployment committed successfully: {rows_loaded}")

        except Exception as e:
            conn.rollback()
            logger.error(f"Deployment failed, rolled back: {e}")
            raise

        # VACUUM outside transaction
        conn.autocommit = True
        for table in load_order:
            cur.execute(sql.SQL("VACUUM ANALYZE {}").format(sql.Identifier(table)))
            logger.info(f"Vacuumed {table}")
```
- **Rationale:** Single transaction ensures all-or-nothing deployment. If any table fails, entire deployment rolls back to pre-deployment state. Validates data before commit (e.g., locations > 0).

### Issue 4: No Logging or Audit Trail
- **Location:** All files (deploy_to_db.py, etl_psgc.py)
- **Impact:** **HIGH**
- **Category:** Observability
- **Description:** The entire pipeline uses only `print()` statements. No structured logging, no log files, no timestamps, no severity levels. When deployment fails in production, there's no audit trail to determine what happened, when, or why. No record of which tables loaded successfully before failure.
- **Current Behavior:**
```python
# deploy_to_db.py:15-17
def run_etl(workbook: Path, reference_year: int, source_label: str) -> Path:
    print("Running ETL...")  # No timestamp, no log level, not persistent
    # ...
```
- **Recommended Fix:**
```python
import logging
from datetime import datetime
from pathlib import Path

# Configure structured logging at module level
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / f'deployment_{datetime.now():%Y%m%d_%H%M%S}.log'),
        logging.StreamHandler()  # Also print to console
    ]
)
logger = logging.getLogger(__name__)

def run_etl(workbook: Path, reference_year: int, source_label: str) -> Path:
    """Run ETL pipeline to generate normalized CSVs."""
    logger.info(f"Starting ETL: workbook={workbook}, year={reference_year}, source={source_label}")

    try:
        df = etl_psgc.load_psgc(workbook)
        logger.info(f"Loaded {len(df)} rows from workbook")

        etl_psgc.export_tables(df, reference_year, source_label)
        logger.info(f"Exported tables to {etl_psgc.OUTPUT_DIR}")

        return etl_psgc.OUTPUT_DIR

    except Exception as e:
        logger.error(f"ETL failed: {e}", exc_info=True)
        raise

def apply_schema(conninfo: str, schema_file: Path) -> None:
    """Apply database schema (idempotent)."""
    logger.info(f"Applying schema from {schema_file}")

    try:
        sql = schema_file.read_text()
        with psycopg.connect(conninfo, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        logger.info("Schema applied successfully")

    except Exception as e:
        logger.error(f"Schema application failed: {e}", exc_info=True)
        raise

# Usage in main()
def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)

    logger.info("="*60)
    logger.info("PSGC Deployment Pipeline Started")
    logger.info(f"Workbook: {args.workbook}")
    logger.info(f"Database: {args.database_url[:30]}...")  # Don't log full credentials
    logger.info("="*60)

    start_time = datetime.now()

    try:
        # ... deployment logic with logging at each step

        duration = (datetime.now() - start_time).total_seconds()
        logger.info(f"Deployment completed successfully in {duration:.1f}s")

    except Exception as e:
        logger.error(f"Deployment failed: {e}", exc_info=True)
        raise SystemExit(1)
```
- **Rationale:** Structured logging enables troubleshooting, provides audit trail for compliance, captures errors with stack traces, and supports monitoring/alerting integration.

### Issue 5: No Post-Deployment Validation
- **Location:** deploy_to_db.py:140 (main function ends without validation)
- **Impact:** **HIGH**
- **Category:** Safety
- **Description:** After deployment completes, there's no verification that data loaded correctly. Silent failures are possible: empty tables, wrong row counts, broken foreign keys, orphaned records. Production users could receive corrupted data without detection.
- **Recommended Fix:**
```python
def validate_deployment(conninfo: str) -> dict:
    """Run smoke tests after deployment to verify data integrity."""
    logger.info("Running post-deployment validation...")

    validations = {}

    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            # Check 1: Row counts
            expected_counts = {
                'locations': (43000, 44000),  # Expected range
                'population_stats': (43000, 44000),
                'city_classifications': (100, 200),
                'income_classifications': (1500, 2000),
                'settlement_tags': (40000, 43000),
            }

            for table, (min_rows, max_rows) in expected_counts.items():
                cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table)))
                count = cur.fetchone()[0]

                if count < min_rows or count > max_rows:
                    raise ValueError(
                        f"{table} row count {count} outside expected range [{min_rows}, {max_rows}]"
                    )

                validations[f"{table}_count"] = count
                logger.info(f"✓ {table}: {count} rows (expected {min_rows}-{max_rows})")

            # Check 2: Hierarchy integrity (no orphans except regions)
            cur.execute("""
                SELECT COUNT(*) FROM locations
                WHERE level_code != 'Reg' AND parent_psgc IS NULL
            """)
            orphans = cur.fetchone()[0]
            if orphans > 0:
                raise ValueError(f"Found {orphans} orphaned non-region locations")
            logger.info(f"✓ Hierarchy integrity: 0 orphaned locations")
            validations['orphaned_locations'] = 0

            # Check 3: Foreign key integrity
            cur.execute("""
                SELECT COUNT(*) FROM population_stats ps
                LEFT JOIN locations l ON ps.psgc_code = l.psgc_code
                WHERE l.psgc_code IS NULL
            """)
            dangling_fk = cur.fetchone()[0]
            if dangling_fk > 0:
                raise ValueError(f"Found {dangling_fk} population_stats with missing locations")
            logger.info(f"✓ Foreign key integrity: 0 dangling references")
            validations['dangling_foreign_keys'] = 0

            # Check 4: Data quality - population coverage
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE ps.psgc_code IS NOT NULL) as with_pop,
                    COUNT(*) as total
                FROM locations l
                LEFT JOIN population_stats ps ON l.psgc_code = ps.psgc_code
            """)
            with_pop, total = cur.fetchone()
            coverage = with_pop / total * 100

            if coverage < 95:
                logger.warning(f"⚠ Population coverage only {coverage:.1f}% (expected >95%)")
            else:
                logger.info(f"✓ Population coverage: {coverage:.1f}%")
            validations['population_coverage_pct'] = coverage

            # Check 5: Sample query (top 5 provinces)
            cur.execute("""
                SELECT l.name, ps.population
                FROM population_stats ps
                JOIN locations l ON l.psgc_code = ps.psgc_code
                WHERE ps.reference_year = 2024 AND l.level_code = 'Prov'
                ORDER BY ps.population DESC LIMIT 5
            """)
            top_provinces = cur.fetchall()

            if not top_provinces:
                raise ValueError("No provinces found in top population query")

            logger.info(f"✓ Top province: {top_provinces[0][0]} ({top_provinces[0][1]:,})")
            validations['top_province'] = top_provinces[0][0]

    logger.info("✓ All validation checks passed")
    return validations

# In main()
def main(argv: Sequence[str] | None = None) -> None:
    # ... existing deployment logic

    # Validate after deployment
    validation_results = validate_deployment(conninfo)

    # Write validation results to file for monitoring
    import json
    validation_file = Path("logs") / f"validation_{datetime.now():%Y%m%d_%H%M%S}.json"
    with validation_file.open("w") as f:
        json.dump(validation_results, f, indent=2)
    logger.info(f"Validation results written to {validation_file}")
```
- **Rationale:** Post-deployment validation catches silent failures, provides confidence in data quality, enables automated alerting on failures, and creates audit trail of deployment success.

### Issue 6: Unsafe SQL String Formatting (SQL Injection Risk)
- **Location:** deploy_to_db.py:64, 70
- **Impact:** **MEDIUM** (mitigated by controlled inputs, but poor pattern)
- **Category:** Security
- **Description:** Table names are inserted directly into SQL strings using f-strings: `f"TRUNCATE TABLE {table} CASCADE"`. While currently safe because `table` comes from hardcoded `load_order` list, this pattern is inherently unsafe and could enable SQL injection if refactored to accept user input.
- **Current Behavior:**
```python
# deploy_to_db.py:64, 70
cur.execute(f"TRUNCATE TABLE {table} CASCADE")  # Unsafe pattern
with cur.copy(
    f"COPY {table} {column_sql} FROM STDIN WITH (FORMAT csv, HEADER true)"
) as copy:
```
- **Recommended Fix:**
```python
from psycopg import sql

def copy_csv(conninfo: str, table: str, csv_path: Path) -> None:
    # ... existing setup code

    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur, csv_path.open("r", encoding="utf-8") as fh:
            # Safe SQL identifier composition
            cur.execute(
                sql.SQL("TRUNCATE TABLE {} CASCADE").format(sql.Identifier(table))
            )

            columns = COPY_COLUMNS.get(table)
            if columns:
                column_list = sql.SQL(", ").join(map(sql.Identifier, columns))
                copy_sql = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(
                    sql.Identifier(table),
                    column_list
                )
            else:
                copy_sql = sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv, HEADER true)").format(
                    sql.Identifier(table)
                )

            with cur.copy(copy_sql) as copy:
                while chunk := fh.read(CHUNK_SIZE):
                    copy.write(chunk)
```
- **Rationale:** Using `sql.Identifier()` properly escapes table/column names, preventing SQL injection. Follows psycopg3 best practices. Makes code future-proof against refactoring that might introduce user-controlled table names.

### Issue 7: No Connection Retry Logic for Network Failures
- **Location:** deploy_to_db.py:24, 62 (psycopg.connect calls)
- **Impact:** **MEDIUM**
- **Category:** Resilience
- **Description:** Neon is a serverless database accessed over internet. Transient network failures, connection timeouts, or Neon scaling events can cause connection failures. No retry logic means deployment fails on first transient error, requiring manual re-run.
- **Recommended Fix:**
```python
import time
from typing import Callable, TypeVar

T = TypeVar('T')

def retry_with_backoff(
    func: Callable[[], T],
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    backoff_multiplier: float = 2.0,
    operation_name: str = "operation"
) -> T:
    """Retry function with exponential backoff for transient failures."""
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            if attempt == max_attempts:
                logger.error(f"{operation_name} failed after {max_attempts} attempts")
                raise

            delay = initial_delay * (backoff_multiplier ** (attempt - 1))
            logger.warning(
                f"{operation_name} failed (attempt {attempt}/{max_attempts}): {e}. "
                f"Retrying in {delay:.1f}s..."
            )
            time.sleep(delay)
        except Exception as e:
            # Non-retryable errors (e.g., syntax errors, constraint violations)
            logger.error(f"{operation_name} failed with non-retryable error: {e}")
            raise

def apply_schema(conninfo: str, schema_file: Path) -> None:
    """Apply database schema with retry logic."""
    logger.info(f"Applying schema from {schema_file}")

    def _apply():
        sql = schema_file.read_text()
        with psycopg.connect(conninfo, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        return None

    retry_with_backoff(_apply, max_attempts=3, operation_name="Schema application")
    logger.info("Schema applied successfully")

def copy_csv(conninfo: str, table: str, csv_path: Path) -> None:
    """Load CSV with retry logic for transient failures."""
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found for {table}: {csv_path}")

    def _load():
        with psycopg.connect(conninfo) as conn:
            # ... existing load logic
            pass

    retry_with_backoff(_load, max_attempts=3, operation_name=f"Loading {table}")
```
- **Rationale:** Exponential backoff handles transient network issues, Neon cold starts, or brief unavailability. Distinguishes between retryable (connection) and non-retryable (constraint violation) errors.

### Issue 8: No Environment Validation
- **Location:** deploy_to_db.py:119-122
- **Impact:** **MEDIUM**
- **Category:** Safety
- **Description:** Script checks if `DATABASE_URL` exists but doesn't validate format, SSL requirements, or connectivity. Fails late after ETL runs (wasting time) or with cryptic error messages.
- **Recommended Fix:**
```python
from urllib.parse import urlparse

def validate_environment(args: argparse.Namespace) -> str:
    """Validate environment and return sanitized connection string."""

    # Check DATABASE_URL exists
    if not args.database_url:
        raise SystemExit(
            "ERROR: DATABASE_URL is required.\n"
            "Set environment variable or pass --database-url\n"
            "Example: export DATABASE_URL='postgresql://user:pass@host/db?sslmode=require'"
        )

    conninfo = args.database_url.strip().strip('"').strip("'")

    # Validate URL format
    try:
        parsed = urlparse(conninfo)
    except Exception as e:
        raise SystemExit(f"ERROR: Invalid DATABASE_URL format: {e}")

    if parsed.scheme not in ('postgresql', 'postgres'):
        raise SystemExit(
            f"ERROR: DATABASE_URL must use postgresql:// scheme, got {parsed.scheme}://"
        )

    # Check SSL requirement for Neon
    if 'neon' in parsed.hostname and 'sslmode=require' not in conninfo:
        raise SystemExit(
            "ERROR: Neon connections must use sslmode=require\n"
            "Add '?sslmode=require' to DATABASE_URL"
        )

    # Test connectivity
    logger.info("Testing database connectivity...")
    try:
        with psycopg.connect(conninfo, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                version = cur.fetchone()[0]
                logger.info(f"✓ Connected to: {version.split(',')[0]}")
    except psycopg.OperationalError as e:
        raise SystemExit(f"ERROR: Cannot connect to database: {e}")

    # Check required files exist
    if not args.workbook.exists():
        raise SystemExit(f"ERROR: Workbook not found: {args.workbook}")

    if not args.schema.exists():
        raise SystemExit(f"ERROR: Schema file not found: {args.schema}")

    logger.info("✓ Environment validation passed")
    return conninfo

# In main()
def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)

    # Validate environment before starting ETL
    conninfo = validate_environment(args)

    # ... rest of deployment
```
- **Rationale:** Fail-fast validation prevents wasted ETL execution, provides clear error messages for common misconfigurations, tests connectivity before deployment.

## Deployment Safety Improvements

### Blue-Green Deployment Strategy
For zero-downtime deployments when serving production queries:

```python
def deploy_blue_green(conninfo: str, load_order: list[str], output_dir: Path) -> None:
    """
    Blue-green deployment using staging tables.
    Zero-downtime: production tables remain queryable until atomic swap.
    """
    logger.info("Starting blue-green deployment...")

    with psycopg.connect(conninfo) as conn:
        try:
            with conn.cursor() as cur:
                # Phase 1: Create staging tables (no lock on production)
                logger.info("Creating staging tables...")
                for table in load_order:
                    cur.execute(sql.SQL(
                        "DROP TABLE IF EXISTS {}_staging CASCADE"
                    ).format(sql.Identifier(table)))

                    cur.execute(sql.SQL(
                        "CREATE TABLE {}_staging "
                        "(LIKE {} INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES)"
                    ).format(sql.Identifier(table), sql.Identifier(table)))

                # Phase 2: Load data into staging (production unaffected)
                logger.info("Loading data into staging tables...")
                rows_loaded = {}
                for table in load_order:
                    csv_path = output_dir / f"{table}.csv"
                    with csv_path.open("r", encoding="utf-8") as fh:
                        columns = COPY_COLUMNS.get(table)
                        if columns:
                            column_list = sql.SQL(", ").join(map(sql.Identifier, columns))
                            copy_sql = sql.SQL(
                                "COPY {}_staging ({}) FROM STDIN WITH (FORMAT csv, HEADER true)"
                            ).format(sql.Identifier(table), column_list)
                        else:
                            copy_sql = sql.SQL(
                                "COPY {}_staging FROM STDIN WITH (FORMAT csv, HEADER true)"
                            ).format(sql.Identifier(table))

                        with cur.copy(copy_sql) as copy:
                            while chunk := fh.read(CHUNK_SIZE):
                                copy.write(chunk)

                    cur.execute(sql.SQL("SELECT COUNT(*) FROM {}_staging").format(sql.Identifier(table)))
                    count = cur.fetchone()[0]
                    rows_loaded[table] = count
                    logger.info(f"Staged {table}: {count} rows")

                # Phase 3: Validate staging data
                logger.info("Validating staging data...")
                if rows_loaded['locations'] < 43000:
                    raise ValueError(
                        f"Staging validation failed: locations has only {rows_loaded['locations']} rows"
                    )

                # Phase 4: Atomic swap (brief exclusive lock ~50-100ms)
                logger.info("Performing atomic table swap...")
                for table in load_order:
                    # Rename production to _old, staging to production
                    cur.execute(sql.SQL("ALTER TABLE {} RENAME TO {}_old").format(
                        sql.Identifier(table),
                        sql.Identifier(table)
                    ))
                    cur.execute(sql.SQL("ALTER TABLE {}_staging RENAME TO {}").format(
                        sql.Identifier(table),
                        sql.Identifier(table)
                    ))

                # Commit the swap
                conn.commit()
                logger.info(f"✓ Blue-green swap completed: {rows_loaded}")

        except Exception as e:
            conn.rollback()
            logger.error(f"Blue-green deployment failed, rolling back: {e}")
            raise

        # Phase 5: Drop old tables outside transaction (no bloat in commit log)
        logger.info("Cleaning up old tables...")
        conn.autocommit = True
        for table in load_order:
            try:
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {}_old CASCADE").format(
                    sql.Identifier(table)
                ))
                logger.info(f"Dropped {table}_old")
            except Exception as e:
                logger.warning(f"Failed to drop {table}_old: {e}")

        # VACUUM new tables
        for table in load_order:
            cur.execute(sql.SQL("VACUUM ANALYZE {}").format(sql.Identifier(table)))
            logger.info(f"Vacuumed {table}")

    logger.info("✓ Blue-green deployment completed successfully")
```

**Benefits:**
- Production tables remain queryable during entire load phase
- Atomic rename operations take <100ms (metadata-only)
- Full rollback capability until commit
- Can validate staging data before swap

**Trade-offs:**
- Requires 2x storage during deployment
- Slightly more complex than truncate-and-reload
- Need to handle foreign key recreation after rename

### Checkpoint/Resume Capability
For long-running deployments that may need to resume after failure:

```python
from pathlib import Path
import json

CHECKPOINT_FILE = Path("logs/deployment_checkpoint.json")

class DeploymentCheckpoint:
    """Track deployment progress for resume capability."""

    def __init__(self):
        self.completed_tables = []
        self.failed_table = None
        self.timestamp = None

    def save(self):
        """Save checkpoint to file."""
        CHECKPOINT_FILE.parent.mkdir(exist_ok=True)
        with CHECKPOINT_FILE.open("w") as f:
            json.dump({
                'completed_tables': self.completed_tables,
                'failed_table': self.failed_table,
                'timestamp': self.timestamp
            }, f, indent=2)

    @classmethod
    def load(cls):
        """Load checkpoint from file."""
        if not CHECKPOINT_FILE.exists():
            return None

        with CHECKPOINT_FILE.open("r") as f:
            data = json.load(f)

        checkpoint = cls()
        checkpoint.completed_tables = data['completed_tables']
        checkpoint.failed_table = data['failed_table']
        checkpoint.timestamp = data['timestamp']
        return checkpoint

    def mark_completed(self, table: str):
        """Mark table as successfully loaded."""
        self.completed_tables.append(table)
        self.timestamp = datetime.now().isoformat()
        self.save()

    def mark_failed(self, table: str):
        """Mark table as failed."""
        self.failed_table = table
        self.timestamp = datetime.now().isoformat()
        self.save()

def deploy_with_resume(conninfo: str, load_order: list[str], output_dir: Path) -> None:
    """Deploy with checkpoint/resume capability."""

    # Check for existing checkpoint
    checkpoint = DeploymentCheckpoint.load()

    if checkpoint:
        logger.warning(f"Found checkpoint from {checkpoint.timestamp}")
        logger.warning(f"Completed tables: {checkpoint.completed_tables}")
        logger.warning(f"Failed table: {checkpoint.failed_table}")

        response = input("Resume from checkpoint? [y/N]: ")
        if response.lower() != 'y':
            logger.info("Starting fresh deployment")
            checkpoint = DeploymentCheckpoint()
            CHECKPOINT_FILE.unlink(missing_ok=True)
        else:
            logger.info(f"Resuming from checkpoint, skipping {len(checkpoint.completed_tables)} tables")
    else:
        checkpoint = DeploymentCheckpoint()

    # Deploy tables
    for table in load_order:
        if table in checkpoint.completed_tables:
            logger.info(f"Skipping {table} (already loaded)")
            continue

        try:
            logger.info(f"Loading {table}...")
            copy_csv(conninfo, table, output_dir / f"{table}.csv")
            checkpoint.mark_completed(table)
            logger.info(f"✓ {table} loaded successfully")

        except Exception as e:
            checkpoint.mark_failed(table)
            logger.error(f"✗ {table} failed: {e}")
            logger.error(f"Checkpoint saved. Run again to resume from {table}")
            raise

    # Clear checkpoint on complete success
    CHECKPOINT_FILE.unlink(missing_ok=True)
    logger.info("✓ All tables loaded, checkpoint cleared")
```

**Use case:** For very large datasets or unreliable network connections where full re-deployment is too expensive.

## Error Handling & Resilience

### Comprehensive Error Handling
```python
class DeploymentError(Exception):
    """Base exception for deployment failures."""
    pass

class ETLError(DeploymentError):
    """ETL pipeline failures."""
    pass

class SchemaError(DeploymentError):
    """Schema application failures."""
    pass

class DataLoadError(DeploymentError):
    """Data loading failures."""
    pass

class ValidationError(DeploymentError):
    """Post-deployment validation failures."""
    pass

def main(argv: Sequence[str] | None = None) -> None:
    """Main deployment orchestrator with comprehensive error handling."""

    args = parse_args(argv)
    start_time = datetime.now()

    try:
        # Phase 1: Environment validation
        logger.info("="*60)
        logger.info("Phase 1: Environment Validation")
        logger.info("="*60)
        conninfo = validate_environment(args)

        # Phase 2: ETL
        logger.info("="*60)
        logger.info("Phase 2: ETL Pipeline")
        logger.info("="*60)
        try:
            output_dir = run_etl(args.workbook, args.reference_year, args.source_label)
        except Exception as e:
            raise ETLError(f"ETL pipeline failed: {e}") from e

        # Phase 3: Schema application
        logger.info("="*60)
        logger.info("Phase 3: Schema Application")
        logger.info("="*60)
        try:
            apply_schema(conninfo, args.schema)
        except Exception as e:
            raise SchemaError(f"Schema application failed: {e}") from e

        # Phase 4: Data loading
        logger.info("="*60)
        logger.info("Phase 4: Data Loading")
        logger.info("="*60)
        try:
            deploy_all_tables(conninfo, load_order, output_dir)
        except Exception as e:
            raise DataLoadError(f"Data loading failed: {e}") from e

        # Phase 5: Validation
        logger.info("="*60)
        logger.info("Phase 5: Post-Deployment Validation")
        logger.info("="*60)
        try:
            validation_results = validate_deployment(conninfo)
        except Exception as e:
            raise ValidationError(f"Validation failed: {e}") from e

        # Success
        duration = (datetime.now() - start_time).total_seconds()
        logger.info("="*60)
        logger.info(f"✓ DEPLOYMENT COMPLETED SUCCESSFULLY in {duration:.1f}s")
        logger.info(f"✓ Tables loaded: {validation_results}")
        logger.info("="*60)

    except ValidationError as e:
        logger.error("="*60)
        logger.error("✗ DEPLOYMENT FAILED: Validation errors")
        logger.error(f"✗ {e}")
        logger.error("="*60)
        logger.error("Action required: Investigate data quality issues")
        logger.error("Rollback: Restore from Neon snapshot or re-run deployment")
        raise SystemExit(1)

    except DataLoadError as e:
        logger.error("="*60)
        logger.error("✗ DEPLOYMENT FAILED: Data loading errors")
        logger.error(f"✗ {e}")
        logger.error("="*60)
        logger.error("Action required: Check database logs, verify CSVs")
        logger.error("Rollback: Database should have rolled back to pre-deployment state")
        raise SystemExit(1)

    except SchemaError as e:
        logger.error("="*60)
        logger.error("✗ DEPLOYMENT FAILED: Schema errors")
        logger.error(f"✗ {e}")
        logger.error("="*60)
        logger.error("Action required: Verify schema.sql syntax, check permissions")
        raise SystemExit(1)

    except ETLError as e:
        logger.error("="*60)
        logger.error("✗ DEPLOYMENT FAILED: ETL errors")
        logger.error(f"✗ {e}")
        logger.error("="*60)
        logger.error("Action required: Verify workbook format, check ETL logic")
        raise SystemExit(1)

    except Exception as e:
        logger.error("="*60)
        logger.error("✗ DEPLOYMENT FAILED: Unexpected error")
        logger.error(f"✗ {e}", exc_info=True)
        logger.error("="*60)
        raise SystemExit(1)
```

### Retry Logic with Circuit Breaker
For production resilience against transient failures:

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Too many failures, reject immediately
    HALF_OPEN = "half_open"  # Testing if service recovered

@dataclass
class CircuitBreaker:
    """Circuit breaker pattern for database operations."""

    failure_threshold: int = 3
    timeout_duration: int = 60  # seconds

    def __post_init__(self):
        self.failure_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED

    def call(self, func: Callable[[], T]) -> T:
        """Execute function with circuit breaker protection."""

        if self.state == CircuitState.OPEN:
            if datetime.now() - self.last_failure_time > timedelta(seconds=self.timeout_duration):
                logger.info("Circuit breaker: Transitioning to HALF_OPEN")
                self.state = CircuitState.HALF_OPEN
            else:
                raise Exception(f"Circuit breaker OPEN: Too many failures, try again later")

        try:
            result = func()

            # Success: reset on successful operation
            if self.state == CircuitState.HALF_OPEN:
                logger.info("Circuit breaker: Service recovered, transitioning to CLOSED")
                self.state = CircuitState.CLOSED
                self.failure_count = 0

            return result

        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            self.failure_count += 1
            self.last_failure_time = datetime.now()

            if self.failure_count >= self.failure_threshold:
                logger.error(
                    f"Circuit breaker: Opening circuit after {self.failure_count} failures"
                )
                self.state = CircuitState.OPEN

            raise

# Global circuit breaker for database operations
db_circuit_breaker = CircuitBreaker(failure_threshold=3, timeout_duration=60)

def copy_csv_with_resilience(conninfo: str, table: str, csv_path: Path) -> None:
    """Load CSV with retry logic and circuit breaker."""

    def _load():
        # Retry logic wraps the actual load
        return retry_with_backoff(
            lambda: _actual_load(conninfo, table, csv_path),
            max_attempts=3,
            operation_name=f"Loading {table}"
        )

    # Circuit breaker wraps retry logic
    return db_circuit_breaker.call(_load)
```

## Security Hardening

### Secrets Management
Current `.env` file approach is acceptable for development but needs improvement for production:

```python
import os
from pathlib import Path

def get_database_url() -> str:
    """
    Get database URL with validation and security checks.
    Supports multiple secret sources (env file, cloud secrets, env vars).
    """

    # Priority order: explicit env var > cloud secrets > .env file

    # 1. Check environment variable (highest priority)
    url = os.getenv("DATABASE_URL")

    # 2. Check cloud secrets (AWS Secrets Manager, GCP Secret Manager, etc.)
    if not url:
        try:
            url = get_from_cloud_secrets()
        except Exception as e:
            logger.debug(f"Cloud secrets not available: {e}")

    # 3. Fall back to .env file (development only)
    if not url and Path(".env").exists():
        logger.warning("Using DATABASE_URL from .env file (development only)")
        from dotenv import load_dotenv
        load_dotenv()
        url = os.getenv("DATABASE_URL")

    if not url:
        raise SystemExit(
            "ERROR: DATABASE_URL not found.\n"
            "Set via: export DATABASE_URL='...' or .env file"
        )

    # Validate URL format
    from urllib.parse import urlparse, parse_qs

    try:
        parsed = urlparse(url)
    except Exception as e:
        raise ValueError(f"Invalid DATABASE_URL format: {e}")

    # Security checks
    if parsed.scheme not in ('postgresql', 'postgres'):
        raise ValueError(f"DATABASE_URL must use postgresql:// scheme")

    # Require SSL for production
    query_params = parse_qs(parsed.query)
    if 'sslmode' not in query_params or query_params['sslmode'][0] != 'require':
        raise ValueError(
            "DATABASE_URL must include '?sslmode=require' for secure connections"
        )

    # Warn if password in plaintext (should use IAM or certificate auth in production)
    if parsed.password:
        logger.warning(
            "Database password in connection string. "
            "Consider using IAM authentication or certificate-based auth for production."
        )

    return url

def get_from_cloud_secrets() -> str:
    """
    Retrieve DATABASE_URL from cloud secret manager.
    Implement based on your cloud provider.
    """
    # Example for AWS Secrets Manager
    try:
        import boto3
        client = boto3.client('secretsmanager')
        response = client.get_secret_value(SecretId='psgc/database-url')
        return response['SecretString']
    except ImportError:
        raise Exception("boto3 not installed")
    except Exception as e:
        raise Exception(f"Failed to retrieve from AWS Secrets Manager: {e}")

# Example for GitHub Actions secrets
def get_from_github_env() -> str:
    """Retrieve from GitHub Actions environment."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise Exception("DATABASE_URL not set in GitHub environment")
    return url
```

### Connection Security Hardening
```python
def create_secure_connection(conninfo: str) -> psycopg.Connection:
    """
    Create database connection with security hardening.
    """
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(conninfo)
    params = parse_qs(parsed.query)

    # Enforce SSL
    if 'sslmode' not in params:
        logger.warning("Adding sslmode=require to connection string")
        conninfo += "&sslmode=require"

    # Connection parameters for security and resilience
    conn_params = {
        'conninfo': conninfo,
        'connect_timeout': 30,  # Fail fast on connection issues
        'options': '-c statement_timeout=300s',  # 5 minute query timeout
    }

    # Additional security for production
    if 'neon' in parsed.hostname:
        # Neon-specific optimizations
        conn_params['options'] += ' -c idle_in_transaction_session_timeout=120s'

    conn = psycopg.connect(**conn_params)

    # Verify SSL connection
    with conn.cursor() as cur:
        cur.execute("SELECT ssl_is_used()")
        ssl_used = cur.fetchone()[0]
        if not ssl_used:
            conn.close()
            raise Exception("SSL connection required but not established")
        logger.info("✓ SSL connection verified")

    return conn
```

### Least Privilege Database Access
```sql
-- Create separate users for deployment vs. application access

-- Deployment user (used by deploy_to_db.py)
CREATE USER psgc_deploy WITH PASSWORD 'secure_deploy_password';
GRANT CONNECT ON DATABASE philippine_standard_geographic_code TO psgc_deploy;
GRANT CREATE, TRUNCATE ON ALL TABLES IN SCHEMA public TO psgc_deploy;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO psgc_deploy;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO psgc_deploy;

-- Read-only application user (used by API)
CREATE USER psgc_app_ro WITH PASSWORD 'secure_readonly_password';
GRANT CONNECT ON DATABASE philippine_standard_geographic_code TO psgc_app_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO psgc_app_ro;
GRANT USAGE ON SCHEMA public TO psgc_app_ro;

-- Ensure future tables inherit permissions
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO psgc_app_ro;

-- Analytics user (for reporting queries)
CREATE USER psgc_analytics WITH PASSWORD 'secure_analytics_password';
GRANT CONNECT ON DATABASE philippine_standard_geographic_code TO psgc_analytics;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO psgc_analytics;
-- Limit to prevent expensive queries
ALTER USER psgc_analytics SET statement_timeout = '60s';
```

## CI/CD Automation

### GitHub Actions Deployment Workflow
```yaml
# .github/workflows/deploy.yml
name: Deploy PSGC Database

on:
  workflow_dispatch:
    inputs:
      environment:
        description: 'Target environment'
        required: true
        type: choice
        options:
          - staging
          - production
      workbook_url:
        description: 'URL to PSGC workbook (or leave empty to use repo version)'
        required: false
        type: string
      deployment_strategy:
        description: 'Deployment strategy'
        required: true
        type: choice
        options:
          - blue-green
          - truncate-reload
        default: blue-green

  # Optional: Scheduled deployment for quarterly PSA releases
  schedule:
    - cron: '0 2 1 */3 *'  # 2 AM on 1st of every 3rd month

jobs:
  validate:
    name: Validate Environment
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install --upgrade pip
          pip install pandas openpyxl psycopg[binary] ruff mypy

      - name: Lint code
        run: ruff check .

      - name: Type check
        run: mypy deploy_to_db.py etl_psgc.py --ignore-missing-imports

      - name: Validate schema syntax
        run: |
          sudo apt-get install -y postgresql-client
          psql --version
          # Dry-run schema validation
          cat schema.sql | grep -v "^--" | head -20

  deploy:
    name: Deploy to ${{ inputs.environment }}
    runs-on: ubuntu-latest
    needs: validate
    environment: ${{ inputs.environment }}

    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install --upgrade pip
          pip install pandas openpyxl 'psycopg[binary]'

      - name: Download workbook (if URL provided)
        if: inputs.workbook_url != ''
        run: |
          wget -O PSGC-latest.xlsx "${{ inputs.workbook_url }}"
          echo "WORKBOOK_PATH=PSGC-latest.xlsx" >> $GITHUB_ENV

      - name: Use repository workbook
        if: inputs.workbook_url == ''
        run: |
          echo "WORKBOOK_PATH=PSGC-3Q-2025-Publication-Datafile.xlsx" >> $GITHUB_ENV

      - name: Backup database (Neon snapshot)
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          NEON_API_KEY: ${{ secrets.NEON_API_KEY }}
        run: |
          # Create Neon branch snapshot before deployment
          # Requires neonctl CLI or API call
          echo "Creating pre-deployment snapshot..."
          # neonctl branches create --name "backup-$(date +%Y%m%d-%H%M%S)"

      - name: Run deployment
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: |
          python deploy_to_db.py \
            --workbook "${{ env.WORKBOOK_PATH }}" \
            --reference-year 2024 \
            --source-label "2024 POPCEN (PSA)"

      - name: Run smoke tests
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: |
          # Basic connectivity test
          python -c "
          import psycopg
          import os
          conn = psycopg.connect(os.getenv('DATABASE_URL'))
          cur = conn.cursor()
          cur.execute('SELECT COUNT(*) FROM locations')
          count = cur.fetchone()[0]
          print(f'Locations count: {count}')
          assert count > 43000, 'Expected >43k locations'
          print('✓ Smoke tests passed')
          "

      - name: Upload deployment logs
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: deployment-logs-${{ inputs.environment }}
          path: logs/
          retention-days: 30

      - name: Notify on success
        if: success()
        uses: 8398a7/action-slack@v3
        with:
          status: ${{ job.status }}
          text: |
            ✓ PSGC deployment to ${{ inputs.environment }} succeeded
            Workbook: ${{ env.WORKBOOK_PATH }}
            Strategy: ${{ inputs.deployment_strategy }}
          webhook_url: ${{ secrets.SLACK_WEBHOOK }}

      - name: Notify on failure
        if: failure()
        uses: 8398a7/action-slack@v3
        with:
          status: ${{ job.status }}
          text: |
            ✗ PSGC deployment to ${{ inputs.environment }} FAILED
            Check logs: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
          webhook_url: ${{ secrets.SLACK_WEBHOOK }}

      - name: Rollback on failure
        if: failure()
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          NEON_API_KEY: ${{ secrets.NEON_API_KEY }}
        run: |
          echo "Deployment failed, initiating rollback..."
          # Restore from Neon snapshot created at start
          # neonctl branches restore --branch main --timestamp "$(date -u -d '30 minutes ago' +'%Y-%m-%d %H:%M:%S')"
```

### Pre-Deployment Validation Script
```python
# scripts/validate_deployment.py
"""
Pre-deployment validation script.
Run before actual deployment to catch issues early.
"""

import sys
from pathlib import Path
import pandas as pd

def validate_workbook(path: Path) -> bool:
    """Validate workbook format and contents."""
    print(f"Validating workbook: {path}")

    try:
        # Check file exists and is readable
        if not path.exists():
            print(f"✗ Workbook not found: {path}")
            return False

        # Load workbook
        xl = pd.ExcelFile(path)

        # Check PSGC sheet exists
        if "PSGC" not in xl.sheet_names:
            print(f"✗ PSGC sheet not found. Available: {xl.sheet_names}")
            return False

        # Load PSGC sheet
        df = pd.read_excel(path, sheet_name="PSGC")

        # Check required columns
        required_cols = [
            "10-digit PSGC",
            "Name",
            "Geographic Level",
            "2024 Population"
        ]

        missing_cols = set(required_cols) - set(df.columns)
        if missing_cols:
            print(f"✗ Missing required columns: {missing_cols}")
            return False

        # Check row count
        if len(df) < 40000:
            print(f"✗ Expected >40k rows, got {len(df)}")
            return False

        # Check data quality
        null_codes = df["10-digit PSGC"].isna().sum()
        if null_codes > 100:
            print(f"✗ Too many null PSGC codes: {null_codes}")
            return False

        print(f"✓ Workbook validation passed:")
        print(f"  - Rows: {len(df):,}")
        print(f"  - Null PSGC codes: {null_codes}")
        print(f"  - Columns: {len(df.columns)}")

        return True

    except Exception as e:
        print(f"✗ Validation failed: {e}")
        return False

def validate_schema(path: Path) -> bool:
    """Validate schema.sql syntax."""
    print(f"Validating schema: {path}")

    try:
        if not path.exists():
            print(f"✗ Schema file not found: {path}")
            return False

        sql = path.read_text()

        # Basic syntax checks
        if "CREATE TABLE" not in sql:
            print("✗ No CREATE TABLE statements found")
            return False

        if "locations" not in sql:
            print("✗ locations table not found in schema")
            return False

        # Check for common mistakes
        if "TRUNCATE" in sql:
            print("⚠ Warning: TRUNCATE found in schema (should be in deploy script)")

        # Count expected tables
        expected_tables = [
            "geographic_levels",
            "city_class_types",
            "income_brackets",
            "urban_rural_tags",
            "locations",
            "population_stats",
            "city_classifications",
            "income_classifications",
            "settlement_tags"
        ]

        for table in expected_tables:
            if table not in sql:
                print(f"✗ Expected table not found: {table}")
                return False

        print(f"✓ Schema validation passed")
        return True

    except Exception as e:
        print(f"✗ Schema validation failed: {e}")
        return False

def main():
    """Run all validations."""
    print("="*60)
    print("Pre-Deployment Validation")
    print("="*60)

    all_passed = True

    # Validate workbook
    workbook_path = Path("PSGC-3Q-2025-Publication-Datafile.xlsx")
    if not validate_workbook(workbook_path):
        all_passed = False

    print()

    # Validate schema
    schema_path = Path("schema.sql")
    if not validate_schema(schema_path):
        all_passed = False

    print("="*60)
    if all_passed:
        print("✓ ALL VALIDATIONS PASSED")
        print("="*60)
        sys.exit(0)
    else:
        print("✗ VALIDATION FAILURES DETECTED")
        print("="*60)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

## Monitoring & Observability

### Deployment Metrics Collection
```python
# monitoring/metrics.py
"""
Deployment metrics collection and reporting.
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

@dataclass
class DeploymentMetrics:
    """Track deployment performance and outcomes."""

    # Timing
    start_time: str
    end_time: Optional[str] = None
    etl_duration_seconds: Optional[float] = None
    schema_duration_seconds: Optional[float] = None
    load_duration_seconds: Optional[float] = None
    total_duration_seconds: Optional[float] = None

    # Data volumes
    workbook_path: str = ""
    workbook_size_mb: float = 0.0
    csv_total_size_mb: float = 0.0

    # Row counts
    tables_loaded: Dict[str, int] = None
    total_rows_loaded: int = 0

    # Outcomes
    success: bool = False
    errors: list = None
    validation_results: Dict = None

    # Environment
    environment: str = "development"
    database_host: str = ""
    python_version: str = ""

    def __post_init__(self):
        if self.tables_loaded is None:
            self.tables_loaded = {}
        if self.errors is None:
            self.errors = []
        if self.validation_results is None:
            self.validation_results = {}

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    def save(self, path: Path) -> None:
        """Save metrics to JSON file."""
        path.parent.mkdir(exist_ok=True, parents=True)
        with path.open("w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)

    @classmethod
    def load(cls, path: Path):
        """Load metrics from JSON file."""
        with path.open("r") as f:
            data = json.load(f)
        return cls(**data)

class MetricsCollector:
    """Collect and report deployment metrics."""

    def __init__(self):
        self.metrics = DeploymentMetrics(
            start_time=datetime.now().isoformat(),
            python_version=sys.version.split()[0]
        )
        self._phase_start = None

    def start_phase(self, phase_name: str):
        """Mark start of deployment phase."""
        self._phase_start = datetime.now()
        logger.info(f"Starting phase: {phase_name}")

    def end_phase(self, phase_name: str):
        """Mark end of deployment phase."""
        if self._phase_start:
            duration = (datetime.now() - self._phase_start).total_seconds()

            if phase_name == "ETL":
                self.metrics.etl_duration_seconds = duration
            elif phase_name == "Schema":
                self.metrics.schema_duration_seconds = duration
            elif phase_name == "Load":
                self.metrics.load_duration_seconds = duration

            logger.info(f"Completed phase {phase_name} in {duration:.1f}s")

    def record_table_load(self, table: str, row_count: int):
        """Record table load metrics."""
        self.metrics.tables_loaded[table] = row_count
        self.metrics.total_rows_loaded += row_count

    def record_error(self, error: str):
        """Record deployment error."""
        self.metrics.errors.append({
            'timestamp': datetime.now().isoformat(),
            'error': str(error)
        })

    def record_validation(self, results: dict):
        """Record validation results."""
        self.metrics.validation_results = results

    def finalize(self, success: bool):
        """Finalize metrics collection."""
        self.metrics.end_time = datetime.now().isoformat()
        self.metrics.success = success

        start = datetime.fromisoformat(self.metrics.start_time)
        end = datetime.fromisoformat(self.metrics.end_time)
        self.metrics.total_duration_seconds = (end - start).total_seconds()

        # Save to file
        metrics_file = Path("logs") / f"metrics_{start:%Y%m%d_%H%M%S}.json"
        self.metrics.save(metrics_file)
        logger.info(f"Metrics saved to {metrics_file}")

        # Print summary
        self.print_summary()

    def print_summary(self):
        """Print metrics summary."""
        print()
        print("="*60)
        print("DEPLOYMENT METRICS SUMMARY")
        print("="*60)
        print(f"Status: {'✓ SUCCESS' if self.metrics.success else '✗ FAILED'}")
        print(f"Duration: {self.metrics.total_duration_seconds:.1f}s")
        print(f"  - ETL: {self.metrics.etl_duration_seconds:.1f}s")
        print(f"  - Schema: {self.metrics.schema_duration_seconds:.1f}s")
        print(f"  - Load: {self.metrics.load_duration_seconds:.1f}s")
        print(f"Total rows loaded: {self.metrics.total_rows_loaded:,}")
        print(f"Tables loaded: {len(self.metrics.tables_loaded)}")
        for table, count in self.metrics.tables_loaded.items():
            print(f"  - {table}: {count:,}")

        if self.metrics.errors:
            print(f"Errors: {len(self.metrics.errors)}")
            for err in self.metrics.errors[:5]:  # Show first 5
                print(f"  - {err['error']}")

        print("="*60)

# Usage in deploy_to_db.py
def main(argv: Sequence[str] | None = None) -> None:
    """Main deployment orchestrator with metrics collection."""

    metrics = MetricsCollector()

    try:
        # ETL phase
        metrics.start_phase("ETL")
        output_dir = run_etl(...)
        metrics.end_phase("ETL")

        # Schema phase
        metrics.start_phase("Schema")
        apply_schema(...)
        metrics.end_phase("Schema")

        # Load phase
        metrics.start_phase("Load")
        for table in load_order:
            # ... load table
            metrics.record_table_load(table, row_count)
        metrics.end_phase("Load")

        # Validation
        validation_results = validate_deployment(...)
        metrics.record_validation(validation_results)

        metrics.finalize(success=True)

    except Exception as e:
        metrics.record_error(e)
        metrics.finalize(success=False)
        raise
```

### Health Check Endpoint
For monitoring deployment status and data freshness:

```python
# api/health.py
"""
Health check endpoint for deployment monitoring.
Can be integrated with FastAPI, Flask, or standalone.
"""

from datetime import datetime
from pathlib import Path
import json
import psycopg

def get_deployment_health(conninfo: str) -> dict:
    """
    Check deployment health and data freshness.
    Returns metrics for monitoring systems.
    """

    health = {
        'status': 'unknown',
        'timestamp': datetime.now().isoformat(),
        'checks': {}
    }

    try:
        with psycopg.connect(conninfo, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                # Check 1: Database connectivity
                cur.execute("SELECT version()")
                db_version = cur.fetchone()[0]
                health['checks']['database_connection'] = {
                    'status': 'healthy',
                    'version': db_version.split(',')[0]
                }

                # Check 2: Table existence
                cur.execute("""
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name IN ('locations', 'population_stats')
                """)
                table_count = cur.fetchone()[0]

                if table_count == 2:
                    health['checks']['schema'] = {'status': 'healthy'}
                else:
                    health['checks']['schema'] = {
                        'status': 'unhealthy',
                        'error': f'Expected 2 core tables, found {table_count}'
                    }

                # Check 3: Data freshness
                cur.execute("""
                    SELECT MAX(collected_at) FROM population_stats
                """)
                last_update = cur.fetchone()[0]

                if last_update:
                    age_days = (datetime.now().date() - last_update).days
                    health['checks']['data_freshness'] = {
                        'status': 'healthy' if age_days < 120 else 'warning',
                        'last_update': last_update.isoformat(),
                        'age_days': age_days
                    }

                # Check 4: Row counts
                cur.execute("SELECT COUNT(*) FROM locations")
                loc_count = cur.fetchone()[0]

                cur.execute("SELECT COUNT(*) FROM population_stats")
                pop_count = cur.fetchone()[0]

                health['checks']['row_counts'] = {
                    'status': 'healthy' if loc_count > 43000 else 'unhealthy',
                    'locations': loc_count,
                    'population_stats': pop_count
                }

                # Check 5: Query performance
                start = datetime.now()
                cur.execute("""
                    SELECT l.name, ps.population
                    FROM population_stats ps
                    JOIN locations l ON l.psgc_code = ps.psgc_code
                    WHERE ps.reference_year = 2024 AND l.level_code = 'Prov'
                    ORDER BY ps.population DESC LIMIT 5
                """)
                query_duration = (datetime.now() - start).total_seconds()

                health['checks']['query_performance'] = {
                    'status': 'healthy' if query_duration < 1.0 else 'degraded',
                    'sample_query_duration_seconds': query_duration
                }

        # Overall status
        all_checks = [c['status'] for c in health['checks'].values() if isinstance(c, dict)]
        if all(s == 'healthy' for s in all_checks):
            health['status'] = 'healthy'
        elif any(s == 'unhealthy' for s in all_checks):
            health['status'] = 'unhealthy'
        else:
            health['status'] = 'degraded'

    except Exception as e:
        health['status'] = 'unhealthy'
        health['error'] = str(e)

    return health

# FastAPI integration example
from fastapi import FastAPI
import os

app = FastAPI()

@app.get("/health")
def health_check():
    """Health check endpoint for load balancers and monitoring."""
    conninfo = os.getenv("DATABASE_URL")
    return get_deployment_health(conninfo)

@app.get("/metrics")
def metrics():
    """Prometheus-compatible metrics endpoint."""
    health = get_deployment_health(os.getenv("DATABASE_URL"))

    # Convert to Prometheus format
    metrics_text = []

    if 'row_counts' in health['checks']:
        metrics_text.append(
            f"psgc_locations_total {health['checks']['row_counts']['locations']}"
        )
        metrics_text.append(
            f"psgc_population_stats_total {health['checks']['row_counts']['population_stats']}"
        )

    if 'data_freshness' in health['checks']:
        metrics_text.append(
            f"psgc_data_age_days {health['checks']['data_freshness']['age_days']}"
        )

    if 'query_performance' in health['checks']:
        metrics_text.append(
            f"psgc_sample_query_duration_seconds {health['checks']['query_performance']['sample_query_duration_seconds']}"
        )

    return "\n".join(metrics_text)
```

### Alerting Configuration
```yaml
# monitoring/alerts.yml
# Alert rules for deployment monitoring (Prometheus/Grafana format)

groups:
  - name: psgc_deployment
    interval: 5m
    rules:
      - alert: PSGCDeploymentFailed
        expr: psgc_deployment_success == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "PSGC deployment failed"
          description: "The last PSGC deployment failed. Check logs in {{ $labels.environment }}"

      - alert: PSGCDataStale
        expr: psgc_data_age_days > 120
        for: 1h
        labels:
          severity: warning
        annotations:
          summary: "PSGC data is stale"
          description: "PSGC data has not been updated in {{ $value }} days (expected <120 days)"

      - alert: PSGCLowRowCount
        expr: psgc_locations_total < 43000
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "PSGC location count too low"
          description: "Only {{ $value }} locations in database (expected >43,000)"

      - alert: PSGCSlowQueries
        expr: psgc_sample_query_duration_seconds > 2.0
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "PSGC queries running slow"
          description: "Sample query taking {{ $value }}s (expected <1s). Check indexes."

      - alert: PSGCDatabaseDown
        expr: up{job="psgc_health_check"} == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "PSGC database unreachable"
          description: "Cannot connect to PSGC database in {{ $labels.environment }}"
```

## Infrastructure as Code

### Terraform for Neon Provisioning
```hcl
# terraform/main.tf
# Infrastructure as Code for PSGC database on Neon

terraform {
  required_providers {
    neon = {
      source  = "kislerdm/neon"
      version = "~> 0.2"
    }
  }

  backend "s3" {
    bucket = "psgc-terraform-state"
    key    = "neon/terraform.tfstate"
    region = "ap-southeast-1"
  }
}

provider "neon" {
  api_key = var.neon_api_key
}

variable "neon_api_key" {
  description = "Neon API key"
  type        = string
  sensitive   = true
}

variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
  default     = "dev"
}

# Neon project
resource "neon_project" "psgc" {
  name   = "psgc-${var.environment}"
  region = "aws-ap-southeast-1"  # Singapore region

  # Production settings
  compute = {
    autoscaling = {
      min_cu = 0.25
      max_cu = 2
    }
  }

  # Enable logical replication for backups
  settings = {
    quota = {
      active_time_seconds    = 3600000  # 1000 hours/month
      compute_time_seconds   = 360000   # 100 hours compute
      written_data_bytes     = 10737418240  # 10 GB
      data_transfer_bytes    = 107374182400  # 100 GB
    }
  }
}

# Main database
resource "neon_database" "main" {
  project_id = neon_project.psgc.id
  name       = "philippine_standard_geographic_code"
  owner_name = "psgc_deploy"
}

# Staging branch for testing
resource "neon_branch" "staging" {
  project_id = neon_project.psgc.id
  parent_id  = neon_project.psgc.default_branch_id
  name       = "staging"
}

# Deployment user
resource "neon_role" "deploy" {
  project_id = neon_project.psgc.id
  branch_id  = neon_project.psgc.default_branch_id
  name       = "psgc_deploy"
}

# Read-only user for API
resource "neon_role" "readonly" {
  project_id = neon_project.psgc.id
  branch_id  = neon_project.psgc.default_branch_id
  name       = "psgc_app_ro"
}

# Outputs
output "database_url" {
  description = "Database connection string"
  value       = "postgresql://${neon_role.deploy.name}@${neon_project.psgc.connection_uri}/${neon_database.main.name}?sslmode=require"
  sensitive   = true
}

output "readonly_url" {
  description = "Read-only connection string for API"
  value       = "postgresql://${neon_role.readonly.name}@${neon_project.psgc.connection_uri}/${neon_database.main.name}?sslmode=require"
  sensitive   = true
}

output "project_id" {
  description = "Neon project ID"
  value       = neon_project.psgc.id
}
```

### Docker Compose for Local Development
```yaml
# docker-compose.yml
# Local development environment with PostgreSQL + PostGIS

version: '3.8'

services:
  postgres:
    image: postgis/postgis:14-3.3
    container_name: psgc_postgres_dev
    environment:
      POSTGRES_DB: philippine_standard_geographic_code
      POSTGRES_USER: psgc_dev
      POSTGRES_PASSWORD: dev_password
      PGDATA: /var/lib/postgresql/data/pgdata
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./schema.sql:/docker-entrypoint-initdb.d/01-schema.sql
      - ./scripts/init-dev-db.sql:/docker-entrypoint-initdb.d/02-init-dev.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U psgc_dev -d philippine_standard_geographic_code"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - psgc_network

  pgadmin:
    image: dpage/pgadmin4:latest
    container_name: psgc_pgadmin
    environment:
      PGADMIN_DEFAULT_EMAIL: admin@psgc.local
      PGADMIN_DEFAULT_PASSWORD: admin
      PGADMIN_CONFIG_SERVER_MODE: 'False'
    ports:
      - "5050:80"
    depends_on:
      - postgres
    networks:
      - psgc_network

volumes:
  pgdata:
    driver: local

networks:
  psgc_network:
    driver: bridge
```

```bash
# scripts/dev-env.sh
#!/bin/bash
# Development environment setup script

set -e

echo "Setting up PSGC development environment..."

# Start Docker Compose
docker-compose up -d

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL to be ready..."
until docker exec psgc_postgres_dev pg_isready -U psgc_dev; do
  sleep 1
done

echo "✓ PostgreSQL is ready"

# Create .env.dev file
cat > .env.dev << EOF
DATABASE_URL="postgresql://psgc_dev:dev_password@localhost:5432/philippine_standard_geographic_code?sslmode=disable"
EOF

echo "✓ Created .env.dev file"

# Run deployment
echo "Running initial deployment..."
source .venv/bin/activate
set -a && source .env.dev && set +a
python deploy_to_db.py

echo "✓ Development environment ready!"
echo ""
echo "Access points:"
echo "  - PostgreSQL: localhost:5432"
echo "  - PgAdmin: http://localhost:5050"
echo "  - Connection string: postgresql://psgc_dev:dev_password@localhost:5432/philippine_standard_geographic_code"
```

## Operational Runbook

### Deployment Checklist

**Pre-Deployment (30 minutes before)**
- [ ] Download latest PSGC workbook from PSA website
- [ ] Verify workbook format with validation script: `python scripts/validate_deployment.py`
- [ ] Create Neon snapshot: `neonctl branches create --name backup-$(date +%Y%m%d)`
- [ ] Announce deployment window to stakeholders (if production)
- [ ] Verify DATABASE_URL secret is current
- [ ] Check disk space on runner/server
- [ ] Review recent deployment logs for patterns

**Deployment Execution**
- [ ] Activate virtual environment: `source .venv/bin/activate`
- [ ] Load environment variables: `set -a && source .env && set +a`
- [ ] Run deployment: `python deploy_to_db.py --workbook PSGC-latest.xlsx`
- [ ] Monitor deployment logs in real-time
- [ ] Watch for error messages or warnings
- [ ] Verify row counts match expectations

**Post-Deployment Validation**
- [ ] Check validation results in logs/validation_*.json
- [ ] Run smoke tests:
  ```bash
  psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM locations;"
  psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM population_stats;"
  ```
- [ ] Test top population query:
  ```sql
  SELECT l.name, ps.population
  FROM population_stats ps
  JOIN locations l ON l.psgc_code = ps.psgc_code
  WHERE ps.reference_year = 2024 AND l.level_code = 'Prov'
  ORDER BY ps.population DESC LIMIT 5;
  ```
- [ ] Check health endpoint: `curl http://api/health`
- [ ] Verify monitoring dashboards show healthy status
- [ ] Review deployment metrics in logs/metrics_*.json

**Communication**
- [ ] Announce deployment completion to stakeholders
- [ ] Update status page (if applicable)
- [ ] Document any issues encountered
- [ ] Update deployment log with outcome

### Rollback Procedure

**Scenario 1: Deployment Failed Before Completion**
```bash
# Database should auto-rollback due to transaction wrapping
# Verify database state:
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM locations;"

# If needed, restore from snapshot:
neonctl branches restore \
  --project-id <project-id> \
  --branch main \
  --timestamp "2024-11-12 10:00:00"

# Verify restoration:
psql "$DATABASE_URL" -c "SELECT MAX(collected_at) FROM population_stats;"
```

**Scenario 2: Deployment Succeeded But Data Issues Discovered**
```bash
# 1. Identify the backup snapshot
neonctl branches list --project-id <project-id>

# 2. Restore from snapshot
neonctl branches restore \
  --project-id <project-id> \
  --branch main \
  --lsn <lsn-from-before-deployment>

# 3. Validate restoration
psql "$DATABASE_URL" -c "
  SELECT
    COUNT(*) as location_count,
    MAX(collected_at) as last_update
  FROM locations l
  LEFT JOIN population_stats ps ON l.psgc_code = ps.psgc_code;
"

# 4. Notify stakeholders
echo "Database rolled back to pre-deployment state"
```

**Scenario 3: Blue-Green Deployment Rollback**
```sql
-- If tables have _old suffix still available:
BEGIN;

-- Swap back to old tables
ALTER TABLE locations RENAME TO locations_failed;
ALTER TABLE locations_old RENAME TO locations;

ALTER TABLE population_stats RENAME TO population_stats_failed;
ALTER TABLE population_stats_old RENAME TO population_stats;

-- Repeat for all tables

COMMIT;

-- Verify
SELECT COUNT(*) FROM locations;
```

### Troubleshooting Guide

**Symptom: Deployment hangs during COPY**
- **Cause:** Network timeout or large CSV
- **Solution:**
  ```bash
  # Check network connectivity
  ping <neon-host>

  # Check CSV file size
  ls -lh data_exports/*.csv

  # If network issue, retry deployment
  python deploy_to_db.py --workbook <file>

  # If CSV too large, increase chunk size
  # Edit deploy_to_db.py: CHUNK_SIZE = 2 << 20  # 2MB
  ```

**Symptom: "relation does not exist" during deployment**
- **Cause:** Concurrent queries during TRUNCATE CASCADE
- **Solution:**
  ```bash
  # Wait 1 minute for queries to finish, retry
  sleep 60
  python deploy_to_db.py --workbook <file>

  # Or implement blue-green deployment (see recommendations)
  ```

**Symptom: Validation fails with low row counts**
- **Cause:** ETL filtered out records or CSV export incomplete
- **Solution:**
  ```bash
  # Check ETL logs
  cat logs/deployment_*.log | grep "ERROR"

  # Inspect CSV files
  wc -l data_exports/*.csv
  head data_exports/locations.csv

  # Re-run ETL only
  python etl_psgc.py --workbook <file>

  # Check CSV row counts vs expected
  ```

**Symptom: Deployment succeeds but queries return no data**
- **Cause:** Tables truncated but COPY failed silently
- **Solution:**
  ```bash
  # Check row counts
  psql "$DATABASE_URL" -c "
    SELECT
      schemaname, tablename, n_live_tup
    FROM pg_stat_user_tables
    WHERE schemaname = 'public';
  "

  # If tables empty, restore from backup
  # (see Rollback Procedure above)
  ```

**Symptom: Permission denied errors**
- **Cause:** Incorrect database user or missing grants
- **Solution:**
  ```bash
  # Check current user
  psql "$DATABASE_URL" -c "SELECT current_user;"

  # Grant necessary permissions
  psql "$DATABASE_URL" -c "
    GRANT CREATE, TRUNCATE ON ALL TABLES IN SCHEMA public TO <user>;
  "
  ```

## Cost Optimization

### Neon Autoscaling Configuration
```python
# Optimize Neon connection settings for cost efficiency

def get_optimized_connection(conninfo: str) -> str:
    """
    Configure connection string for Neon cost optimization.
    """

    # Add connection parameters to reduce idle compute time
    if '?' in conninfo:
        conninfo += '&'
    else:
        conninfo += '?'

    optimizations = [
        'sslmode=require',  # Required for Neon
        'options=-c statement_timeout=60s',  # Prevent runaway queries
        'options=-c idle_in_transaction_session_timeout=120s',  # Clean up idle sessions
        'connect_timeout=30',  # Fail fast on connection issues
    ]

    conninfo += '&'.join(optimizations)
    return conninfo
```

### Data Retention Policy
```sql
-- Archive old population data to reduce storage costs

-- Create archive table for historical data (optional)
CREATE TABLE IF NOT EXISTS population_stats_archive (
    LIKE population_stats INCLUDING ALL
);

-- Move data older than 5 years to archive
BEGIN;

WITH archived AS (
    DELETE FROM population_stats
    WHERE reference_year < EXTRACT(YEAR FROM CURRENT_DATE) - 5
    RETURNING *
)
INSERT INTO population_stats_archive
SELECT * FROM archived;

COMMIT;

-- Compress archive table
VACUUM FULL population_stats_archive;

-- Consider exporting to S3 for long-term archival
```

### Storage Efficiency
```sql
-- Periodic maintenance to reclaim space

-- Run after each deployment to reclaim space from DELETE operations
VACUUM ANALYZE locations;
VACUUM ANALYZE population_stats;
VACUUM ANALYZE city_classifications;
VACUUM ANALYZE income_classifications;
VACUUM ANALYZE settlement_tags;

-- Check table sizes
SELECT
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS total_size,
    pg_size_pretty(pg_relation_size(schemaname||'.'||tablename)) AS table_size,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename) - pg_relation_size(schemaname||'.'||tablename)) AS index_size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

## Questions for Maintainers

1. **Deployment Frequency:** How often will PSGC data be refreshed?
   - Quarterly (aligned with PSA releases) - current truncate-reload acceptable
   - Monthly - need blue-green deployment
   - Real-time - need streaming architecture

2. **Acceptable Downtime:** What is the acceptable downtime window for deployments?
   - Off-peak hours only (2-6 AM PHT)
   - Any time (implement zero-downtime blue-green)
   - No downtime allowed (requires read replicas)

3. **Concurrent Users:** Expected concurrent queries during deployment?
   - <10 - current approach acceptable
   - 10-100 - need blue-green deployment
   - >100 - need read replicas + load balancing

4. **Rollback Requirements:** Maximum time to rollback failed deployment?
   - Manual rollback acceptable (30+ minutes)
   - Automated rollback required (<5 minutes)
   - Must maintain old version during deployment (blue-green)

5. **Monitoring Budget:** Budget for monitoring/alerting services?
   - Free tier only (GitHub Actions, logs)
   - Modest budget (<$50/month) - Prometheus + Grafana
   - Full monitoring stack - DataDog, New Relic

6. **Compliance Requirements:** Any audit logging or compliance needs?
   - None - current logging sufficient
   - Basic - add structured logging
   - Strict - implement full audit trail with immutable logs

7. **Disaster Recovery:** Required RPO (Recovery Point Objective) / RTO (Recovery Time Objective)?
   - RPO: How much data loss is acceptable? (e.g., 24 hours)
   - RTO: How quickly must system recover? (e.g., 4 hours)

## Positive Patterns to Maintain

1. **Idempotent schema with IF NOT EXISTS** (schema.sql)
   - Enables safe re-application during deployments
   - Supports GitOps workflows
   - Prevents "already exists" errors in CI/CD

2. **Streaming COPY protocol** (deploy_to_db.py:69-76)
   - Memory-efficient for large datasets
   - 1MB chunks prevent memory exhaustion
   - Faster than row-by-row INSERT

3. **Explicit column ordering in COPY_COLUMNS** (deploy_to_db.py:31-55)
   - Resilient to CSV column reordering
   - Self-documenting ETL contract
   - Prevents "column count mismatch" errors

4. **Dependency-ordered table loading** (deploy_to_db.py:128-134)
   - Respects foreign key constraints
   - Prevents constraint violation errors
   - Clear documentation of table dependencies

5. **Single-command deployment** (deploy_to_db.py)
   - Reduces human error
   - Enables CI/CD automation
   - Consistent deployment process

6. **Separation of ETL and deployment** (etl_psgc.py vs deploy_to_db.py)
   - Testable components
   - Flexible deployment strategies
   - Enables offline ETL development

## Implementation Roadmap

### Phase 1: Critical Safety (Week 1-2) - REQUIRED BEFORE PRODUCTION
**Estimated effort:** 40-60 hours

**Priority: CRITICAL - Cannot deploy to production without these**

- [ ] **Transaction management** (Issue #1)
  - Wrap table loads in transactions
  - Add explicit commit/rollback
  - Test rollback on failure
  - *Impact:* Prevents partial failure leaving database inconsistent
  - *Effort:* 4 hours

- [ ] **Deployment safety** (Issue #2)
  - Replace TRUNCATE with DELETE for concurrency
  - Add VACUUM after DELETE
  - OR implement blue-green deployment
  - *Impact:* Eliminates query failures during deployment
  - *Effort:* 8 hours (DELETE) or 16 hours (blue-green)

- [ ] **Rollback capability** (Issue #3)
  - Implement all-tables-in-transaction deployment
  - Add validation before commit
  - Document Neon snapshot rollback procedure
  - *Impact:* Enables recovery from failed deployments
  - *Effort:* 6 hours

- [ ] **Structured logging** (Issue #4)
  - Replace print() with logging module
  - Add log files with timestamps
  - Log errors with stack traces
  - *Impact:* Enables troubleshooting and audit trail
  - *Effort:* 6 hours

- [ ] **Post-deployment validation** (Issue #5)
  - Implement validation checks
  - Verify row counts and data quality
  - Fail deployment on validation errors
  - *Impact:* Catches silent data corruption
  - *Effort:* 8 hours

- [ ] **SQL injection prevention** (Issue #6)
  - Use sql.Identifier for table/column names
  - Update all SQL string formatting
  - *Impact:* Future-proofs against security vulnerabilities
  - *Effort:* 4 hours

- [ ] **Connection retry logic** (Issue #7)
  - Implement exponential backoff
  - Distinguish retryable vs non-retryable errors
  - *Impact:* Resilience against transient network failures
  - *Effort:* 4 hours

- [ ] **Environment validation** (Issue #8)
  - Validate DATABASE_URL format
  - Test connectivity before deployment
  - Check required files exist
  - *Impact:* Fail-fast on misconfiguration
  - *Effort:* 4 hours

**Deliverables:**
- Updated deploy_to_db.py with all safety fixes
- Test suite for deployment scenarios
- Deployment documentation with rollback procedure
- Log analysis showing successful deployment with metrics

**Success Criteria:**
- Deployment succeeds or fails atomically (no partial state)
- Failed deployment rolls back automatically
- All operations logged with timestamps
- Concurrent queries succeed during deployment

---

### Phase 2: Automation (Week 3-4) - REQUIRED FOR PRODUCTION OPERATIONS
**Estimated effort:** 30-40 hours

**Priority: HIGH - Needed for reliable production operations**

- [ ] **GitHub Actions workflow**
  - Create deployment workflow
  - Add pre-deployment validation
  - Implement approval gates for production
  - *Impact:* Automated, consistent deployments
  - *Effort:* 8 hours

- [ ] **Dev/staging/prod environments**
  - Separate Neon branches or projects
  - Environment-specific secrets
  - Test on staging before prod
  - *Impact:* Safe testing before production
  - *Effort:* 6 hours

- [ ] **CI/CD pipeline**
  - Automated validation on commit
  - Linting and type checking
  - Integration tests
  - *Impact:* Catch errors before deployment
  - *Effort:* 10 hours

- [ ] **Smoke tests**
  - Automated post-deployment tests
  - Query performance validation
  - Data quality checks
  - *Impact:* Automated verification
  - *Effort:* 6 hours

**Deliverables:**
- .github/workflows/deploy.yml
- Environment configuration (dev/staging/prod)
- Automated test suite
- Deployment runbook

**Success Criteria:**
- One-click deployment via GitHub Actions
- Automated tests pass on every commit
- Staging environment mirrors production
- Failed deployments automatically roll back

---

### Phase 3: Production Hardening (Week 5-7) - REQUIRED FOR PUBLIC-FACING
**Estimated effort:** 40-50 hours

**Priority: HIGH - Needed before exposing to external users**

- [ ] **Monitoring and alerting**
  - Deployment metrics collection
  - Health check endpoint
  - Alert configuration (Slack/email)
  - *Impact:* Visibility into deployment status
  - *Effort:* 12 hours

- [ ] **Secrets management**
  - Move from .env to cloud secrets
  - Rotate database credentials
  - Implement least privilege access
  - *Impact:* Production-grade security
  - *Effort:* 8 hours

- [ ] **Operational runbooks**
  - Deployment checklist
  - Rollback procedures
  - Troubleshooting guide
  - *Impact:* Team can operate without author
  - *Effort:* 8 hours

- [ ] **Health check endpoints**
  - FastAPI health endpoint
  - Prometheus metrics endpoint
  - Data freshness monitoring
  - *Impact:* Automated health monitoring
  - *Effort:* 10 hours

**Deliverables:**
- Monitoring dashboard (Grafana/similar)
- Alert rules and notification channels
- Operational runbook documentation
- Health check API

**Success Criteria:**
- Deployment failures trigger alerts within 5 minutes
- Health status visible in dashboard
- Runbook enables on-call engineer to deploy
- Secrets rotated without code changes

---

### Phase 4: Advanced Features (Week 8+) - OPTIONAL ENHANCEMENTS
**Estimated effort:** 40-60 hours

**Priority: MEDIUM - Nice-to-have for operational excellence**

- [ ] **Infrastructure as Code**
  - Terraform configuration for Neon
  - Automated provisioning
  - Environment replication
  - *Effort:* 12 hours

- [ ] **Docker Compose local dev**
  - PostgreSQL + PostGIS containers
  - PgAdmin for database inspection
  - Local deployment testing
  - *Effort:* 8 hours

- [ ] **Load testing**
  - Deployment performance benchmarks
  - Concurrent query testing
  - Database scaling limits
  - *Effort:* 12 hours

- [ ] **Disaster recovery testing**
  - Quarterly restore from backup
  - RPO/RTO validation
  - Failover procedures
  - *Effort:* 16 hours

**Deliverables:**
- terraform/ directory with IaC
- docker-compose.yml for local dev
- Load testing suite and results
- Disaster recovery runbook

**Success Criteria:**
- New environment provisioned in <1 hour
- Local development mirrors production
- Load tests validate 100 concurrent users
- Disaster recovery tested quarterly

---

## Production Readiness Assessment

**Overall Score:** 4.5/10

### Operational Metrics

**Deployment Safety:** 3/10
- ✗ No transaction boundaries (partial failures leave inconsistent state)
- ✗ Truncate-and-reload blocks concurrent queries
- ✗ No rollback mechanism
- ✓ Idempotent schema design
- ✓ Dependency-ordered loading

**Error Resilience:** 2/10
- ✗ No retry logic for transient failures
- ✗ No error recovery mechanisms
- ✗ Silent failures possible (no validation)
- ✗ No graceful degradation
- Minimal error handling

**Security Posture:** 5/10
- ✓ SSL connection requirement checked
- ✗ SQL injection risk (string formatting)
- ✗ Credentials in .env file (not production-ready)
- ✗ No secrets rotation
- ✗ No access control (single user)
- ✗ Credentials visible in logs

**Observability:** 2/10
- ✗ No structured logging
- ✗ Only print() statements
- ✗ No metrics collection
- ✗ No health checks
- ✗ No alerting
- ✗ No audit trail

**Automation:** 4/10
- ✓ Single-command deployment
- ✓ ETL automation
- ✗ No CI/CD pipeline
- ✗ Manual deployment required
- ✗ No automated testing
- ✗ No environment separation

---

### Gaps to Production

**CRITICAL (Blocks production launch):**
1. Transaction management - prevents partial failure state
2. Concurrent query safety - eliminates deployment downtime
3. Rollback capability - enables recovery from failures
4. Structured logging - required for troubleshooting
5. Post-deployment validation - catches data corruption

**HIGH (Must fix within 1 month of launch):**
1. Retry logic - resilience against transient failures
2. SQL injection prevention - security hardening
3. Secrets management - production-grade security
4. Monitoring/alerting - operational visibility
5. CI/CD automation - reliable deployments

**MEDIUM (Should fix within 3 months):**
1. Blue-green deployment - zero-downtime deployments
2. Health check endpoints - monitoring integration
3. Operational runbooks - team enablement
4. Dev/staging environments - safe testing

**LOW (Future enhancements):**
1. Infrastructure as Code - reproducibility
2. Docker local dev - developer experience
3. Load testing - capacity planning
4. Disaster recovery - business continuity

---

### Performance Baseline (Estimated)

**Current Deployment Performance:**
- ETL duration: ~30-60 seconds (43k rows)
- Schema application: ~2-5 seconds
- Data load: ~60-120 seconds (5 tables, 130k total rows)
- **Total deployment time: 2-3 minutes**

**Failure Scenarios:**
- Network timeout during COPY: Database left with truncated tables
- Schema error: Previous tables lost, new tables not created
- Validation failure: Bad data loaded, no detection
- Concurrent query during TRUNCATE: Query fails with "relation does not exist"

**After Phase 1 Fixes:**
- Transaction rollback: <1 second
- Deployment failure: Original data intact
- Concurrent queries: Zero downtime
- Validation catches bad data: Deployment rejected

---

### Scalability Assessment

**Current Capacity:**
- Dataset size: ~130k rows across 5 tables
- Deployment frequency: Quarterly (acceptable for current approach)
- Concurrent users: 0 (development only)
- Geographic scope: Single region (Neon Singapore)

**Production Scaling Needs:**
- Dataset growth: +10% annually (~150k rows by 2026)
- Deployment frequency: Monthly (need blue-green)
- Concurrent users: 10-100 (need zero-downtime)
- Geographic scope: Philippines + international API consumers

**Scaling Limits:**
- Truncate-and-reload breaks at >10 concurrent users
- No read replicas for geographic distribution
- No CDN for static responses
- No rate limiting for API protection

---

## Production Go-Live Criteria

### Minimum Viable Production (MVP)
To deploy to production serving real users, these are **REQUIRED**:

✅ **Critical Safety (Phase 1 complete):**
- [x] Transaction management implemented
- [x] Concurrent query safety (DELETE or blue-green)
- [x] Rollback capability tested
- [x] Structured logging with log files
- [x] Post-deployment validation

✅ **Basic Operations:**
- [x] CI/CD pipeline with automated deployment
- [x] Staging environment for testing
- [x] Health check endpoint
- [x] Basic monitoring (row counts, deployment status)

✅ **Documentation:**
- [x] Deployment runbook
- [x] Rollback procedure
- [x] Troubleshooting guide

### Recommended Production (Full Stack)
For production-grade deployment with SLA commitments:

✅ **All MVP criteria above, PLUS:**

✅ **Advanced Safety (Phase 3):**
- [x] Blue-green deployment (zero downtime)
- [x] Automated rollback on validation failure
- [x] Secrets management (cloud secrets, not .env)
- [x] Least privilege access control

✅ **Comprehensive Monitoring:**
- [x] Deployment metrics collection
- [x] Alerting on failures (Slack/PagerDuty)
- [x] Data freshness monitoring
- [x] Query performance tracking

✅ **Team Enablement:**
- [x] On-call runbook
- [x] Team trained on deployment procedure
- [x] Disaster recovery plan tested

---

## Immediate Action Items (Priority Order)

**Week 1 Actions (CRITICAL):**
1. ✅ Implement transaction management (4 hours)
   - Modify deploy_to_db.py to use transactions
   - Test rollback on failure

2. ✅ Add structured logging (6 hours)
   - Replace print() with logging module
   - Create logs/ directory
   - Log all operations with timestamps

3. ✅ Implement post-deployment validation (8 hours)
   - Add validation checks
   - Test row counts and data quality
   - Fail deployment on errors

**Week 2 Actions (HIGH):**
4. ✅ Fix deployment concurrency (8-16 hours)
   - Replace TRUNCATE with DELETE (simpler)
   - OR implement blue-green deployment (better)
   - Test with concurrent queries

5. ✅ Add retry logic (4 hours)
   - Implement exponential backoff
   - Handle transient network failures

6. ✅ Prevent SQL injection (4 hours)
   - Use sql.Identifier throughout
   - Test with edge cases

**Week 3-4 Actions (CI/CD):**
7. ✅ Create GitHub Actions workflow (8 hours)
8. ✅ Setup staging environment (6 hours)
9. ✅ Add smoke tests (6 hours)

---

## Estimated Timeline to Production

### Fast Track (6 weeks)
**Scope:** Minimum viable production with basic safety
- Week 1-2: Critical safety fixes (Phase 1)
- Week 3-4: CI/CD automation (Phase 2)
- Week 5-6: Basic monitoring + runbooks (Phase 3 minimal)
- **Total effort:** ~100 hours

### Recommended Path (8 weeks)
**Scope:** Full production readiness with comprehensive monitoring
- Week 1-2: Critical safety fixes (Phase 1)
- Week 3-4: CI/CD automation (Phase 2)
- Week 5-7: Production hardening (Phase 3)
- Week 8: Testing and documentation
- **Total effort:** ~140 hours

### Conservative Path (12 weeks)
**Scope:** Enterprise-grade with disaster recovery
- Week 1-2: Critical safety fixes (Phase 1)
- Week 3-4: CI/CD automation (Phase 2)
- Week 5-7: Production hardening (Phase 3)
- Week 8-10: Advanced features (Phase 4)
- Week 11-12: Load testing + DR testing
- **Total effort:** ~180 hours

---

## Recommendation

**For Current State (Development):**
The system is **suitable for development and internal testing** but requires **critical safety improvements before production deployment**.

**For Production Deployment:**
**Minimum:** Complete Phase 1 (2 weeks, 40-60 hours)
**Recommended:** Complete Phase 1-3 (7 weeks, 110-150 hours)
**Enterprise:** Complete all phases (12 weeks, 180 hours)

**Priority Recommendation:**
Start with **Phase 1 critical fixes immediately** (Week 1-2). These are **mandatory for any production deployment** and prevent data loss/corruption. The system can go to production after Phase 1 + basic Phase 2 (CI/CD), but ongoing operations will be manual and risky without Phase 3 monitoring.

**Final Assessment:**
With proper investment in infrastructure hardening (8-10 weeks), this system can achieve production-grade reliability and operational excellence. The foundational design (idempotent schema, streaming COPY, clean separation of concerns) is solid; it primarily needs operational safety layers (transactions, logging, monitoring, automation) to be production-ready.
