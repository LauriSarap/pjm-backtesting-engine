#!/usr/bin/env python3
"""Audit locally pulled PJM data for coverage, gaps, and duplicates. Requires: pandas."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

# Data lives outside the package: $PJM_DATA_ROOT if set, else ./data
DATA_ROOT = Path(os.environ.get("PJM_DATA_ROOT", "data")).resolve()
RAW = DATA_ROOT / "raw" / "pjm"
DEFAULT_MD = DATA_ROOT / "pjm-data-audit.md"
DEFAULT_JSON = DATA_ROOT / "pjm_data_audit.json"


STUDY_ZONES = {"PJM-RTO", "COMED", "PSEG", "DOM", "AEP"}


@dataclass(frozen=True)
class AuditSpec:
    area: str
    feed: str
    path: Path
    date_col: str | None = None
    interval_minutes: int | None = None
    group_cols: list[str] = field(default_factory=list)
    key_cols: list[str] | None = None
    required_cols: list[str] = field(default_factory=list)
    expected_groups: set[str] | None = None
    expected_start_utc: str | None = None
    notes: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit locally pulled PJM data.")
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MD)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    return parser.parse_args()


def csv_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.glob("*.csv"))


def read_header(path: Path) -> list[str]:
    return list(pd.read_csv(path, nrows=0).columns)


def parse_datetimes(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce", format="mixed")


def audit_spec(spec: AuditSpec) -> dict[str, Any]:
    files = csv_files(spec.path)
    result: dict[str, Any] = {
        "area": spec.area,
        "feed": spec.feed,
        "path": str(spec.path),
        "files": len(files),
        "rows": 0,
        "empty_files": [],
        "missing_required_columns": [],
        "first_timestamp": None,
        "last_timestamp": None,
        "unique_timestamps": None,
        "global_gap_count": None,
        "max_global_gap_minutes": None,
        "duplicate_key_rows": None,
        "groups": [],
        "missing_expected_groups": [],
        "expected_start_ok": None,
        "status": "ok",
        "notes": spec.notes,
    }
    if not files:
        result["status"] = "fail"
        result["notes"] = append_note(result["notes"], "No CSV files found.")
        return result

    frames: list[pd.DataFrame] = []
    all_columns: set[str] = set()
    for file in files:
        header = read_header(file)
        all_columns.update(header)
        missing = [col for col in spec.required_cols if col not in header]
        if missing:
            result["missing_required_columns"].append({"file": str(file), "columns": missing})
            continue
        usecols = sorted(
            {
                col
                for col in [
                    spec.date_col,
                    *spec.group_cols,
                    *(spec.key_cols or []),
                    *spec.required_cols,
                ]
                if col and col in header
            }
        )
        frame = pd.read_csv(file, usecols=usecols or None, dtype=str)
        if frame.empty:
            result["empty_files"].append(str(file))
        result["rows"] += int(len(frame))
        if spec.date_col and spec.date_col in frame.columns and not frame.empty:
            frame[spec.date_col] = parse_datetimes(frame[spec.date_col])
        frames.append(frame)

    if result["missing_required_columns"]:
        result["status"] = "fail"
        return result
    if not frames:
        result["status"] = "fail"
        result["notes"] = append_note(result["notes"], "No readable CSV frames.")
        return result

    data = pd.concat(frames, ignore_index=True)

    if spec.date_col and spec.date_col in data.columns and not data.empty:
        timestamps = data[spec.date_col].dropna()
        if timestamps.empty:
            result["status"] = "warn"
            result["notes"] = append_note(
                result["notes"],
                "Date column was present but all values failed datetime parsing.",
            )
        else:
            first = timestamps.min()
            last = timestamps.max()
            result["first_timestamp"] = first.isoformat()
            result["last_timestamp"] = last.isoformat()
            unique = pd.Series(timestamps.unique()).sort_values(ignore_index=True)
            result["unique_timestamps"] = int(len(unique))
            if spec.interval_minutes and len(unique) > 1:
                deltas = unique.diff().dropna().dt.total_seconds().div(60)
                gap_deltas = deltas[deltas != spec.interval_minutes]
                result["global_gap_count"] = int(len(gap_deltas))
                result["max_global_gap_minutes"] = (
                    float(gap_deltas.max()) if not gap_deltas.empty else None
                )
                if not gap_deltas.empty:
                    result["status"] = "warn"
            if spec.expected_start_utc:
                result["expected_start_ok"] = first == pd.Timestamp(spec.expected_start_utc)
                if not result["expected_start_ok"]:
                    result["status"] = "warn"

    key_cols = (
        spec.key_cols
        if spec.key_cols is not None
        else (
            [*([spec.date_col] if spec.date_col else []), *spec.group_cols]
            if spec.group_cols
            else []
        )
    )
    if key_cols and not data.empty and all(col in data.columns for col in key_cols):
        result["duplicate_key_rows"] = int(data.duplicated(key_cols).sum())
        if result["duplicate_key_rows"]:
            result["status"] = "warn"

    if spec.group_cols and not data.empty and all(col in data.columns for col in spec.group_cols):
        group_counts = (
            data.groupby(spec.group_cols, dropna=False)
            .size()
            .reset_index(name="rows")
            .sort_values("rows", ascending=False)
        )
        result["groups"] = group_counts.head(30).to_dict("records")
        if spec.expected_groups and len(spec.group_cols) == 1:
            observed = set(str(value) for value in data[spec.group_cols[0]].dropna().unique())
            missing = sorted(spec.expected_groups - observed)
            result["missing_expected_groups"] = missing
            if missing:
                result["status"] = "fail"

    if result["empty_files"]:
        result["status"] = "warn" if result["status"] == "ok" else result["status"]

    return result


def append_note(existing: str, note: str) -> str:
    return f"{existing} {note}".strip()


def specs() -> list[AuditSpec]:
    zone = RAW / "zone_lmps"
    anc = RAW / "ancillary_services"
    context = RAW / "context"
    cap = RAW / "capacity_market"
    return [
        AuditSpec(
            "zone_lmps",
            "da_hrl_lmps",
            zone / "da_hrl_lmps",
            "datetime_beginning_utc",
            60,
            ["pnode_name"],
            None,
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "pnode_name",
                "total_lmp_da",
            ],
            STUDY_ZONES,
            "2021-01-01T05:00:00",
        ),
        AuditSpec(
            "zone_lmps",
            "rt_fivemin_mnt_lmps",
            zone / "rt_fivemin_mnt_lmps",
            "datetime_beginning_utc",
            5,
            ["pnode_name"],
            None,
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "pnode_name",
                "total_lmp_rt",
            ],
            STUDY_ZONES,
            "2021-01-01T05:00:00",
            notes="Settlements-verified RT currently lags the last two operating days.",
        ),
        AuditSpec(
            "zone_lmps",
            "rt_fivemin_hrl_lmps",
            zone / "rt_fivemin_hrl_lmps",
            "datetime_beginning_utc",
            5,
            ["pnode_name"],
            None,
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "pnode_name",
                "total_lmp_rt",
            ],
            STUDY_ZONES,
            notes="Preliminary/current RT tail used until settlements-verified feed catches up.",
        ),
        AuditSpec(
            "ancillary_services",
            "da_ancillary_services",
            anc / "da_ancillary_services",
            "datetime_beginning_utc",
            60,
            ["ancillary_service", "unit"],
            None,
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "ancillary_service",
                "unit",
                "value",
            ],
            None,
            "2022-10-01T04:00:00",
        ),
        AuditSpec(
            "ancillary_services",
            "ancillary_services_fivemin_hrl",
            anc / "ancillary_services_fivemin_hrl",
            "datetime_beginning_utc",
            5,
            ["ancillary_service", "unit"],
            None,
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "ancillary_service",
                "unit",
                "value",
            ],
            None,
            "2021-01-01T05:00:00",
        ),
        AuditSpec(
            "ancillary_services",
            "ancillary_services",
            anc / "ancillary_services",
            "datetime_beginning_utc",
            60,
            ["ancillary_service", "unit"],
            None,
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "ancillary_service",
                "unit",
                "value",
            ],
            None,
            "2021-01-01T05:00:00",
        ),
        AuditSpec(
            "ancillary_services",
            "da_reserve_market_results",
            anc / "da_reserve_market_results",
            "datetime_beginning_utc",
            60,
            ["locale", "service"],
            None,
            ["datetime_beginning_utc", "datetime_beginning_ept", "locale", "service"],
            None,
            "2022-10-01T04:00:00",
        ),
        AuditSpec(
            "ancillary_services",
            "reserve_market_results",
            anc / "reserve_market_results",
            "datetime_beginning_utc",
            None,
            ["locale", "service"],
            None,
            ["datetime_beginning_utc", "datetime_beginning_ept", "locale", "service"],
            notes="Mixed hourly and five-minute history around 2022-10-01; audit coverage and duplicate keys only.",
        ),
        AuditSpec(
            "ancillary_services",
            "reg_market_results",
            anc / "reg_market_results",
            "datetime_beginning_utc",
            None,
            [],
            None,
            ["datetime_beginning_utc", "datetime_beginning_ept", "requirement"],
            notes="Hourly before 2025-10-01, half-hourly after regulation redesign.",
        ),
        AuditSpec(
            "ancillary_services",
            "reg_prices",
            anc / "reg_prices",
            "datetime_beginning_utc",
            5,
            [],
            None,
            ["datetime_beginning_utc", "datetime_beginning_ept"],
            notes="Recent feed with limited history; windows before its first available date return zero rows.",
        ),
        AuditSpec(
            "ancillary_services",
            "sync_pri_reserves_buses_list",
            anc / "sync_pri_reserves_buses_list" / "all.csv",
            None,
            None,
            [],
            None,
            ["subzone"],
        ),
        AuditSpec(
            "ancillary_services",
            "sync_pri_reserves_resources_list",
            anc / "sync_pri_reserves_resources_list" / "all.csv",
            None,
            None,
            [],
            None,
            ["resource_id", "subzone"],
        ),
        AuditSpec(
            "capacity",
            "resource_clearing_prices_long",
            cap / "resource_clearing_prices_long.csv",
            None,
            None,
            ["delivery_year", "auction", "lda"],
            ["delivery_year", "auction", "capacity_product_type", "lda"],
            ["delivery_year", "auction", "capacity_product_type", "lda", "raw_value"],
        ),
        AuditSpec(
            "capacity",
            "commitments_by_fuel_type_long",
            cap / "commitments_by_fuel_type_long.csv",
            None,
            None,
            ["delivery_year", "unit", "data_type", "fuel_type"],
            None,
            ["delivery_year", "unit", "data_type", "fuel_type", "mw"],
        ),
        AuditSpec(
            "capacity",
            "pai_balancing_ratios",
            cap / "pai_balancing_ratios.csv",
            "pai_start_time_gmt",
            None,
            [],
            None,
            ["pai_start_time_gmt", "final_balancing_ratio"],
        ),
        AuditSpec(
            "capacity",
            "pai_charge_rates_by_lda",
            cap / "pai_charge_rates_by_lda.csv",
            None,
            None,
            ["delivery_year", "lda"],
            None,
            ["delivery_year", "lda", "pai_charge_rate"],
        ),
        AuditSpec(
            "context",
            "gen_by_fuel",
            context / "gen_by_fuel",
            "datetime_beginning_utc",
            60,
            [],
            None,
            ["datetime_beginning_utc", "datetime_beginning_ept", "fuel_type", "mw"],
            None,
            "2021-01-01T05:00:00",
        ),
        AuditSpec(
            "context",
            "frcstd_gen_outages",
            context / "frcstd_gen_outages",
            "forecast_execution_date_ept",
            1440,
            [],
            None,
            ["forecast_execution_date_ept", "forecast_date"],
            None,
            "2021-01-01T00:00:00",
        ),
        AuditSpec(
            "context",
            "gen_outages_by_type",
            context / "gen_outages_by_type",
            "forecast_execution_date_ept",
            1440,
            ["region"],
            ["forecast_execution_date_ept", "forecast_date", "region"],
            [
                "forecast_execution_date_ept",
                "forecast_date",
                "region",
                "total_outages_mw",
            ],
            None,
            "2021-01-01T00:00:00",
        ),
        AuditSpec(
            "context",
            "day_gen_capacity",
            context / "day_gen_capacity",
            "bid_datetime_beginning_utc",
            60,
            [],
            None,
            ["bid_datetime_beginning_utc", "eco_max", "emerg_max", "total_committed"],
            None,
            "2021-01-01T05:00:00",
        ),
        AuditSpec(
            "context",
            "hrl_load_metered",
            context / "hrl_load_metered",
            "datetime_beginning_utc",
            60,
            ["load_area"],
            None,
            ["datetime_beginning_utc", "datetime_beginning_ept", "load_area", "mw"],
            None,
            "2021-01-01T05:00:00",
            notes="Metered load lags current-day preliminary load.",
        ),
        AuditSpec(
            "context",
            "hrl_load_estimated",
            context / "hrl_load_estimated",
            "datetime_beginning_utc",
            60,
            ["load_area"],
            None,
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "load_area",
                "estimated_load_hourly",
            ],
            None,
            "2021-01-01T05:00:00",
            notes="Estimated load lags current-day preliminary load.",
        ),
        AuditSpec(
            "context",
            "hrl_load_prelim",
            context / "hrl_load_prelim",
            "datetime_beginning_utc",
            60,
            ["load_area"],
            None,
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "load_area",
                "prelim_load_avg_hourly",
            ],
            None,
            "2021-01-01T05:00:00",
        ),
        AuditSpec(
            "context",
            "load_frcstd_hist",
            context / "load_frcstd_hist",
            "evaluated_at_utc",
            None,
            ["forecast_area"],
            ["evaluated_at_utc", "forecast_hour_beginning_utc", "forecast_area"],
            [
                "evaluated_at_utc",
                "evaluated_at_ept",
                "forecast_hour_beginning_utc",
                "forecast_area",
                "forecast_load_mw",
            ],
            notes="Forecasts are every six hours starting one day before effective date; exact evaluation times vary by PJM process.",
        ),
        AuditSpec(
            "context",
            "wind_gen",
            context / "wind_gen",
            "datetime_beginning_utc",
            60,
            ["area"],
            None,
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "area",
                "wind_generation_mw",
            ],
            None,
            "2021-01-01T05:00:00",
        ),
        AuditSpec(
            "context",
            "solar_gen",
            context / "solar_gen",
            "datetime_beginning_utc",
            60,
            ["area"],
            None,
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "area",
                "solar_generation_mw",
            ],
            None,
            "2021-01-01T05:00:00",
        ),
        AuditSpec(
            "context",
            "rt_tempset",
            context / "rt_tempset",
            "datetime_beginning_utc",
            None,
            ["zone"],
            ["datetime_beginning_utc", "zone"],
            [
                "datetime_beginning_utc",
                "datetime_beginning_ept",
                "zone",
                "rt_temperature_set",
            ],
            None,
            "2021-01-01T05:00:00",
            notes="Irregular event-style timestamps; use as observed system state, not a fixed hourly series.",
        ),
        AuditSpec(
            "context",
            "load_frcstd_7_day",
            context / "load_frcstd_7_day",
            "evaluated_at_datetime_utc",
            None,
            ["forecast_area"],
            [
                "evaluated_at_datetime_utc",
                "forecast_datetime_beginning_utc",
                "forecast_area",
            ],
            [
                "evaluated_at_datetime_utc",
                "forecast_datetime_beginning_utc",
                "forecast_area",
                "forecast_load_mw",
            ],
            notes="Current snapshot only.",
        ),
        AuditSpec(
            "context",
            "very_short_load_frcst",
            context / "very_short_load_frcst",
            "evaluated_at_utc",
            5,
            ["forecast_area"],
            ["evaluated_at_utc", "forecast_datetime_beginning_utc", "forecast_area"],
            [
                "evaluated_at_utc",
                "forecast_datetime_beginning_utc",
                "forecast_area",
                "forecast_load_mw",
            ],
            notes="30-day official PJM retention window.",
        ),
        AuditSpec(
            "context",
            "hourly_wind_power_forecast",
            context / "hourly_wind_power_forecast",
            "evaluated_at_utc",
            10,
            [],
            ["evaluated_at_utc", "datetime_beginning_utc"],
            ["evaluated_at_utc", "datetime_beginning_utc", "wind_forecast_mwh"],
            notes="30-day official PJM retention window.",
        ),
        AuditSpec(
            "context",
            "hourly_solar_power_forecast",
            context / "hourly_solar_power_forecast",
            "evaluated_at_utc",
            60,
            [],
            ["evaluated_at_utc", "datetime_beginning_utc"],
            ["evaluated_at_utc", "datetime_beginning_utc", "solar_forecast_mwh"],
            notes="30-day official PJM retention window.",
        ),
        AuditSpec(
            "context",
            "five_min_wind_power_forecast",
            context / "five_min_wind_power_forecast",
            "evaluated_at_utc",
            10,
            [],
            ["evaluated_at_utc", "datetime_beginning_utc"],
            ["evaluated_at_utc", "datetime_beginning_utc", "wind_forecast_mwh"],
            notes="30-day official PJM retention window.",
        ),
        AuditSpec(
            "context",
            "five_min_solar_power_forecast",
            context / "five_min_solar_power_forecast",
            "evaluated_at_utc",
            10,
            [],
            ["evaluated_at_utc", "datetime_beginning_utc"],
            ["evaluated_at_utc", "datetime_beginning_utc", "solar_forecast_mwh"],
            notes="30-day official PJM retention window.",
        ),
        AuditSpec(
            "context",
            "five_min_solar_generation",
            context / "five_min_solar_generation",
            "datetime_beginning_utc",
            None,
            [],
            None,
            ["datetime_beginning_utc", "solar_generation_mw"],
            notes="30-day limited-retention live generation feed; timestamps can be offset by seconds.",
        ),
    ]


def write_markdown(path: Path, results: list[dict[str, Any]], generated_at: str) -> None:
    counts = pd.DataFrame(results).groupby(["area", "status"]).size().to_dict()
    total_rows = sum(int(item["rows"]) for item in results)
    lines = [
        "# PJM Data Audit",
        "",
        f"Generated: {generated_at}.",
        "",
        "## Summary",
        "",
        f"- Audited feeds/tables: {len(results)}.",
        f"- Audited CSV rows: {total_rows:,}.",
        f"- Status counts: {', '.join(f'{area}/{status}: {count}' for (area, status), count in sorted(counts.items()))}.",
        "",
        "## Findings",
        "",
    ]

    bad = [item for item in results if item["status"] != "ok"]
    if not bad:
        lines.append("- No hard missing-file or missing-column failures found.")
    for item in bad:
        lines.append(
            f"- `{item['area']}/{item['feed']}` status `{item['status']}`: "
            f"rows={item['rows']:,}, files={item['files']}, "
            f"first={item['first_timestamp']}, last={item['last_timestamp']}."
        )
        if item["empty_files"]:
            lines.append(f"  Empty files: {len(item['empty_files'])}.")
        if item["missing_required_columns"]:
            lines.append(f"  Missing columns: `{item['missing_required_columns']}`.")
        if item["global_gap_count"]:
            lines.append(
                f"  Global timestamp gaps/non-standard deltas: {item['global_gap_count']} "
                f"(max delta {item['max_global_gap_minutes']} minutes)."
            )
        if item["expected_start_ok"] is False:
            lines.append("  Expected start timestamp mismatch.")
        if item["duplicate_key_rows"]:
            lines.append(f"  Duplicate key rows: {item['duplicate_key_rows']:,}.")
        if item["notes"]:
            lines.append(f"  Note: {item['notes']}")

    lines.extend(
        [
            "",
            "## Feed Coverage",
            "",
            "| Area | Feed | Status | Files | Rows | First | Last | Gap count | Duplicate key rows | Notes |",
            "|---|---|---:|---:|---:|---|---|---:|---:|---|",
        ]
    )
    for item in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    item["area"],
                    f"`{item['feed']}`",
                    f"`{item['status']}`",
                    str(item["files"]),
                    f"{int(item['rows']):,}",
                    str(item["first_timestamp"] or ""),
                    str(item["last_timestamp"] or ""),
                    str(item["global_gap_count"] if item["global_gap_count"] is not None else ""),
                    str(
                        item["duplicate_key_rows"] if item["duplicate_key_rows"] is not None else ""
                    ),
                    item["notes"].replace("|", "/"),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- For regular hourly/five-minute feeds, gap counts are based on unique UTC timestamps across the feed. A nonzero gap can be legitimate for mixed-granularity feeds or limited current snapshots, but should be investigated before modeling that feed as continuous.",
            "- Duplicate key rows are based on the feed's modeled key columns and can be legitimate where PJM republishes versions. For dispatch/backtest tables, filter to current/latest versions when available.",
            "- Node-level settlement LMPs and network constraints are out of scope for the zone-level model.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    args = parse_args()
    generated_at = datetime.now().isoformat(timespec="seconds")
    results = []
    for spec in specs():
        print(f"auditing {spec.area}/{spec.feed}", flush=True)
        results.append(audit_spec(spec))
    total_rows = sum(int(item["rows"]) for item in results)
    counts = pd.DataFrame(results).groupby(["area", "status"]).size().to_dict()
    payload = {
        "generated_at": generated_at,
        "totals": {
            "feeds": len(results),
            "rows": total_rows,
            "status_counts": {
                f"{area}/{status}": count for (area, status), count in sorted(counts.items())
            },
        },
        "feeds": results,
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2) + "\n")
    write_markdown(args.markdown, results, generated_at)
    print(f"wrote {args.json}")
    print(f"wrote {args.markdown}")


if __name__ == "__main__":
    main()
