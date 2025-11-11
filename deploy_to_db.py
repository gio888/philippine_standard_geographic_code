from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

import psycopg

import etl_psgc


def run_etl(workbook: Path, reference_year: int, source_label: str) -> Path:
    print("Running ETL...")
    df = etl_psgc.load_psgc(workbook)
    etl_psgc.export_tables(df, reference_year, source_label)
    return etl_psgc.OUTPUT_DIR


def apply_schema(conninfo: str, schema_file: Path) -> None:
    print("Applying schema...")
    sql = schema_file.read_text()
    with psycopg.connect(conninfo, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    print("Schema applied.")


CHUNK_SIZE = 1 << 20  # 1 MB
COPY_COLUMNS = {
    "locations": [
        "psgc_code",
        "name",
        "level_code",
        "parent_psgc",
        "correspondence_code",
        "status",
        "old_names",
    ],
    "population_stats": ["psgc_code", "reference_year", "population", "source"],
    "city_classifications": ["psgc_code", "class_code", "source"],
    "income_classifications": [
        "psgc_code",
        "bracket_code",
        "source",
        "effective_year",
    ],
    "settlement_tags": [
        "psgc_code",
        "tag_code",
        "source",
        "reference_year",
    ],
}


def copy_csv(conninfo: str, table: str, csv_path: Path) -> None:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found for {table}: {csv_path}")
    print(f"Loading {table} from {csv_path}...")
    with psycopg.connect(conninfo, autocommit=True) as conn:
        with conn.cursor() as cur, csv_path.open("r", encoding="utf-8") as fh:
            cur.execute(f"TRUNCATE TABLE {table} CASCADE")
            columns = COPY_COLUMNS.get(table)
            column_sql = (
                f"({', '.join(columns)})" if columns else ""
            )
            with cur.copy(
                f"COPY {table} {column_sql} FROM STDIN WITH (FORMAT csv, HEADER true)"
            ) as copy:
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    copy.write(chunk)
    print(f"{table} loaded.")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ETL, schema migration, and Neon load for PSGC data."
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        default=Path("PSGC-3Q-2025-Publication-Datafile.xlsx"),
        help="Path to PSA PSGC Excel workbook.",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=Path("schema.sql"),
        help="SQL file defining the database schema.",
    )
    parser.add_argument(
        "--reference-year",
        type=int,
        default=2024,
        help="Population reference year.",
    )
    parser.add_argument(
        "--source-label",
        type=str,
        default="2024 POPCEN (PSA)",
        help="Population source label.",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=os.getenv("DATABASE_URL"),
        help="PostgreSQL connection string (defaults to DATABASE_URL env).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if not args.database_url:
        raise SystemExit(
            "DATABASE_URL is required (set env or pass --database-url)."
        )
    conninfo = args.database_url.strip().strip('"').strip("'")

    output_dir = run_etl(args.workbook, args.reference_year, args.source_label)
    apply_schema(conninfo, args.schema)

    load_order = [
        "locations",
        "population_stats",
        "city_classifications",
        "income_classifications",
        "settlement_tags",
    ]

    for table in load_order:
        csv_path = output_dir / f"{table}.csv"
        copy_csv(conninfo, table, csv_path)

    print("Deployment complete.")


if __name__ == "__main__":
    main(sys.argv[1:])
