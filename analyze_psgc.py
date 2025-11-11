from __future__ import annotations

from pathlib import Path

import pandas as pd


def summarize_sheet_shapes(xl: pd.ExcelFile) -> None:
    print("# Workbook overview")
    for sheet in xl.sheet_names:
        df = xl.parse(sheet)
        print(f"- {sheet}: {len(df):,} rows x {len(df.columns)} columns")
    print()


def analyze_psgc_sheet(psgc: pd.DataFrame) -> None:
    print("# PSGC sheet")
    psgc = psgc.dropna(subset=["10-digit PSGC"])

    level_counts = psgc["Geographic Level"].value_counts()
    print("Geographic level counts:")
    for level, count in level_counts.items():
        print(f"  {level:>6}: {count:,}")
    print()

    print("Population by level (count rows / summed 2024 population):")
    pop = (
        psgc.groupby("Geographic Level")["2024 Population"]
        .agg(["count", "sum"])
        .sort_values("sum", ascending=False)
    )
    print(pop.to_string())
    print()

    for column, title in [
        ("City Class", "City class distribution"),
        (
            "Income\nClassification (DOF DO No. 074.2024)",
            "Income classification distribution",
        ),
        (
            "Urban / Rural\n(based on 2020 CPH)",
            "Urban/rural split (2020 CPH tagging)",
        ),
    ]:
        counts = psgc[column].value_counts(dropna=False)
        print(f"{title}:")
        print(counts.to_string())
        print()

    provinces = psgc.loc[psgc["Geographic Level"] == "Prov"]
    top_prov = provinces.nlargest(5, "2024 Population")[["Name", "2024 Population"]]
    print("Top 5 provinces by 2024 population:")
    print(top_prov.to_string(index=False))
    print()


def clean_table(df: pd.DataFrame, label_column: str) -> pd.DataFrame:
    df = df.dropna(how="all")
    df = df.dropna(subset=[label_column])
    df = df[~df[label_column].astype(str).str.contains("NOTE", case=False)]
    df = df[~df[label_column].isin(list("abc"))]
    return df


def analyze_summary_tables(path: Path) -> None:
    national = pd.read_excel(path, sheet_name="National Summary", header=9)
    national = clean_table(national, "REGION")
    for col in ["PROV.", "CITIES", "MUN.", "BGY.", "POPULATION\n(2024 POPCEN)"]:
        national[col] = pd.to_numeric(national[col], errors="coerce")

    prov_sum = pd.read_excel(path, sheet_name="Prov Sum", header=9)
    prov_sum = clean_table(prov_sum, "NAME")
    for col in ["PROV.", "CITIES", "MUN.", "BGY."]:
        prov_sum[col] = pd.to_numeric(prov_sum[col], errors="coerce")

    nat_view = national[
        ["REGION", "PROV.", "CITIES", "MUN.", "BGY.", "POPULATION\n(2024 POPCEN)"]
    ].set_index("REGION")
    print("# National summary table")
    print(nat_view.to_string())
    print()

    regional_rows = nat_view.drop(index="PHILIPPINES")
    aggregates = regional_rows.sum()
    print(
        "Aggregate counts (sum of regional rows only):",
        {
            "provinces": int(aggregates["PROV."]),
            "cities": int(aggregates["CITIES"]),
            "municipalities": int(aggregates["MUN."]),
            "barangays": int(aggregates["BGY."]),
        },
    )
    print()

    print("# Provincial summary table (first 10 rows)")
    print(prov_sum.head(10).to_string(index=False))
    print()


def main() -> None:
    path = Path("PSGC-3Q-2025-Publication-Datafile.xlsx")
    xl = pd.ExcelFile(path)
    summarize_sheet_shapes(xl)
    analyze_psgc_sheet(xl.parse("PSGC"))
    analyze_summary_tables(path)


if __name__ == "__main__":
    main()
