from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

import pandas as pd

PSGC_SHEET = "PSGC"
OUTPUT_DIR = Path("data_exports")
LEVEL_ORDER = {"Reg": 0, "Prov": 1, "City": 2, "Mun": 2, "SubMun": 3, "Bgy": 4, "Other": 5}


def normalize_code(value: object) -> Optional[str]:
    if pd.isna(value):
        return None
    code = str(value).strip()
    if not code or code.lower() == "nan":
        return None
    digits = "".join(ch for ch in code if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(10)


def candidate_parents(code: str, level: str) -> list[str]:
    region = code[:2] + "00000000"
    province = code[:4] + "000000"
    city_or_mun = code[:6] + "0000"
    submun = code[:8] + "00"

    if level == "Reg":
        return []
    if level == "Prov":
        return [region]
    if level in {"City", "Mun"}:
        return [province, region]
    if level == "SubMun":
        return [city_or_mun, province, region]
    if level == "Bgy":
        return [submun, city_or_mun, province, region]
    return [province, region]


def infer_parent(code: str, level: str, valid_codes: set[str]) -> Optional[str]:
    for candidate in candidate_parents(code, level):
        if candidate != code and candidate in valid_codes:
            return candidate
    return None


def load_psgc(path: Path) -> pd.DataFrame:
    df = pd.read_excel(
        path,
        sheet_name=PSGC_SHEET,
        dtype={
            "10-digit PSGC": str,
            "Correspondence Code": str,
            "2024 Population": "float64",
        },
    )
    df = df.rename(
        columns={
            "10-digit PSGC": "psgc_code",
            "Name": "name",
            "Correspondence Code": "correspondence_code",
            "Geographic Level": "level_code",
            "Old names": "old_names",
            "City Class": "city_class",
            "Income\nClassification (DOF DO No. 074.2024)": "income_class",
            "Urban / Rural\n(based on 2020 CPH)": "urban_rural",
            "2024 Population": "population_2024",
            "Status": "status",
        }
    )
    df = df[df["psgc_code"].notna()]
    df["level_code"] = df["level_code"].fillna("Other")
    df["psgc_code"] = df["psgc_code"].apply(normalize_code)
    return df


def export_tables(df: pd.DataFrame, reference_year: int, source: str) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    valid_codes = {code for code in df["psgc_code"] if code}

    df["parent_psgc"] = df.apply(
        lambda row: infer_parent(row["psgc_code"], row["level_code"], valid_codes),
        axis=1,
    )

    locations = df[
        [
            "psgc_code",
            "name",
            "level_code",
            "parent_psgc",
            "correspondence_code",
            "status",
            "old_names",
        ]
    ].copy()
    locations["old_names"] = locations["old_names"].fillna("").str.strip()
    locations = locations.drop_duplicates(subset=["psgc_code"])
    locations["level_rank"] = locations["level_code"].map(
        LEVEL_ORDER
    ).fillna(LEVEL_ORDER["Prov"])
    locations = locations.sort_values(["level_rank", "psgc_code"]).drop(
        columns=["level_rank"]
    )
    locations.to_csv(OUTPUT_DIR / "locations.csv", index=False)

    population = df[
        ["psgc_code", "population_2024"]
    ].dropna(subset=["population_2024"])
    population = population.rename(columns={"population_2024": "population"})
    population["population"] = population["population"].round().astype(int)
    population["reference_year"] = reference_year
    population["source"] = source
    population = population[
        ["psgc_code", "reference_year", "population", "source"]
    ]
    population.to_csv(OUTPUT_DIR / "population_stats.csv", index=False)

    city_classes = df.dropna(subset=["city_class"])[
        ["psgc_code", "city_class"]
    ].drop_duplicates()
    city_classes = city_classes.rename(columns={"city_class": "class_code"})
    city_classes["source"] = source
    city_classes.to_csv(OUTPUT_DIR / "city_classifications.csv", index=False)

    income = df.dropna(subset=["income_class"])[
        ["psgc_code", "income_class"]
    ].drop_duplicates()
    income = income.rename(columns={"income_class": "bracket_code"})
    income["source"] = "DOF DO 074-2024"
    income["effective_year"] = reference_year
    income.to_csv(OUTPUT_DIR / "income_classifications.csv", index=False)

    settlement = df.dropna(subset=["urban_rural"])[
        ["psgc_code", "urban_rural"]
    ].drop_duplicates()
    settlement = settlement.rename(columns={"urban_rural": "tag_code"})
    settlement["source"] = "2020 CPH"
    settlement["reference_year"] = 2020
    settlement.to_csv(OUTPUT_DIR / "settlement_tags.csv", index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transform PSGC Excel workbook into normalized CSVs."
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        default=Path("PSGC-3Q-2025-Publication-Datafile.xlsx"),
        help="Path to the PSA PSGC Excel workbook.",
    )
    parser.add_argument(
        "--reference-year",
        type=int,
        default=2024,
        help="Reference year for the population figures.",
    )
    parser.add_argument(
        "--source-label",
        type=str,
        default="2024 POPCEN (PSA)",
        help="Source label recorded alongside metrics.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    df = load_psgc(args.workbook)
    export_tables(df, args.reference_year, args.source_label)
    print(f"CSV exports written to {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
