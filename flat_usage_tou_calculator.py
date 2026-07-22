#!/usr/bin/env python3
"""
flat_usage_tou_calculator.py

Parses the retailer-portal "MyUsageData" flat CSV export (one row per
30-minute interval per register - the format that looks like:

    AccountNumber,NMI,DeviceNumber,DeviceType,RegisterCode,
    RateTypeDescription,StartDate,EndDate,ProfileReadValue,
    RegisterReadValue,QualityFlag

) and applies a TOU tariff, same as nem12_tou_calculator.py does for raw
NEM12 files. This is a separate, simpler parser because this export is
already long-format (one row per interval) rather than NEM12's
day-blocked 200/300/400 record structure.

Usage
-----
    python3 flat_usage_tou_calculator.py MyUsageData.csv tariff_config.json \
        --register E1 \
        --out-summary summary.csv \
        --out-detail detail.csv
"""

import argparse
import csv
import sys
from datetime import datetime

# Reuse the tariff engine + detail-writer from the NEM12 script so both
# input formats share identical tariff logic. The summary output below is
# specific to this script (adds Plan Name + a TOU rate-structure summary).
from nem12_tou_calculator import (
    load_tariff, apply_tariff, write_detail_csv,
)


def describe_periods(tariff):
    """
    Build a human-readable list of the TOU periods defined in the tariff
    config: name, which days they apply to, the time window(s), and the
    rate - for display in the summary output alongside the actual costs.
    """
    rows = []
    if "ev_rate" in tariff:
        ev_window = tariff.get("ev_window", {"start": "00:00", "end": "06:00"})
        rows.append({
            "name": "ev",
            "days": "all",
            "months": "",
            "windows": f"{ev_window['start']}-{ev_window['end']}",
            "rate": tariff["ev_rate"],
        })
    for period in tariff.get("periods", []):
        days = period.get("days", "all")
        months = period.get("months")
        windows_str = ", ".join(f"{w['start']}-{w['end']}" for w in period["windows"])
        months_str = ",".join(str(m) for m in months) if months else ""
        rows.append({
            "name": period["name"],
            "days": days,
            "months": months_str,
            "windows": windows_str,
            "rate": period["rate"],
        })
    if "default_rate" in tariff:
        rows.append({
            "name": "default",
            "days": "all",
            "months": "",
            "windows": "(any time not matched above)",
            "rate": tariff["default_rate"],
        })
    return rows


def parse_flat_csv(path):
    """
    Parse the flat retailer usage-export CSV into the same record shape
    used by the NEM12 parser: {nmi, register, date, time, kwh, quality}
    """
    records = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"NMI", "RegisterCode", "StartDate", "ProfileReadValue", "QualityFlag"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"This doesn't look like the expected flat usage CSV - missing columns: {missing}. "
                f"Found columns: {reader.fieldnames}"
            )

        for row in reader:
            # RegisterCode looks like '48395#E1' - the part after '#' is
            # the actual data-stream/register identifier (E1, E2, B1...)
            raw_register = row["RegisterCode"].strip()
            register = raw_register.split("#")[-1] if "#" in raw_register else raw_register

            start = datetime.strptime(row["StartDate"].strip(), "%d/%m/%Y %I:%M:%S %p")

            kwh_str = row["ProfileReadValue"].strip()
            if kwh_str == "":
                continue
            kwh = float(kwh_str)

            quality = row["QualityFlag"].strip() or "A"

            records.append({
                "nmi": row["NMI"].strip(),
                "register": register,
                "date": start.date(),
                "time": start.time(),
                "kwh": kwh,
                "quality": quality,
            })
    return records


def write_summary_csv_with_plan(path, tariff, summary, monthly, supply_charge_total):
    plan_name = tariff.get("plan_name", "(unnamed plan)")
    period_rows = describe_periods(tariff)

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Plan Name", plan_name])
        w.writerow([])

        w.writerow(["-- TOU rate structure --"])
        w.writerow(["period", "days", "months", "time_window(s)", "rate_dollars_per_kwh"])
        for row in period_rows:
            w.writerow([row["name"], row["days"], row["months"], row["windows"], f"{row['rate']:.4f}"])
        if tariff.get("daily_supply_charge_dollars"):
            w.writerow(["daily_supply_charge", "", "", "per day", f"{tariff['daily_supply_charge_dollars']:.4f}"])
        w.writerow([])

        w.writerow(["-- By TOU period (actual usage/cost) --"])
        w.writerow(["period", "kwh", "cost_dollars", "avg_rate"])
        total_kwh = 0.0
        total_cost = 0.0
        for name, vals in sorted(summary.items()):
            avg_rate = vals["cost"] / vals["kwh"] if vals["kwh"] else 0
            w.writerow([name, f"{vals['kwh']:.3f}", f"{vals['cost']:.2f}", f"{avg_rate:.4f}"])
            total_kwh += vals["kwh"]
            total_cost += vals["cost"]
        w.writerow([])

        w.writerow(["-- By month --"])
        w.writerow(["month", "kwh", "cost_dollars"])
        for m, vals in sorted(monthly.items()):
            w.writerow([m, f"{vals['kwh']:.3f}", f"{vals['cost']:.2f}"])
        w.writerow([])

        w.writerow(["-- Totals --"])
        w.writerow(["total_kwh", f"{total_kwh:.3f}"])
        w.writerow(["total_usage_cost_dollars", f"{total_cost:.2f}"])
        if supply_charge_total:
            w.writerow(["total_supply_charge_dollars", f"{supply_charge_total:.2f}"])
            w.writerow(["grand_total_dollars", f"{total_cost + supply_charge_total:.2f}"])
        else:
            w.writerow(["grand_total_dollars", f"{total_cost:.2f}"])


def print_console_summary_with_plan(tariff, summary, monthly, supply_charge_total):
    plan_name = tariff.get("plan_name", "(unnamed plan)")
    period_rows = describe_periods(tariff)

    print(f"\n=== Plan: {plan_name} ===")

    print("\n=== TOU rate structure ===")
    for row in period_rows:
        days_str = row["days"]
        months_str = f", months {row['months']}" if row["months"] else ""
        print(f"  {row['name']:12s}  {row['windows']:28s}  ({days_str}{months_str})   {row['rate']:.4f} $/kWh")
    if tariff.get("daily_supply_charge_dollars"):
        print(f"  {'supply charge':12s}  {'per day':28s}  {'':16s}   {tariff['daily_supply_charge_dollars']:.4f} $/day")

    total_kwh = sum(v["kwh"] for v in summary.values())
    total_cost = sum(v["cost"] for v in summary.values())

    print("\n=== TOU cost breakdown (actual usage) ===")
    for name, vals in sorted(summary.items()):
        avg_rate = vals["cost"] / vals["kwh"] if vals["kwh"] else 0
        print(f"  {name:12s}  {vals['kwh']:10.2f} kWh   ${vals['cost']:9.2f}   (avg {avg_rate:.4f} $/kWh)")

    print("\n=== Monthly totals ===")
    for m, vals in sorted(monthly.items()):
        print(f"  {m}   {vals['kwh']:10.2f} kWh   ${vals['cost']:9.2f}")

    print("\n=== Totals ===")
    print(f"  Usage:          {total_kwh:.2f} kWh")
    print(f"  Usage cost:     ${total_cost:.2f}")
    if supply_charge_total:
        print(f"  Supply charge:  ${supply_charge_total:.2f}")
        print(f"  Grand total:    ${total_cost + supply_charge_total:.2f}")
    else:
        print(f"  Grand total:    ${total_cost:.2f}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("usage_csv", help="Path to your flat 'MyUsageData' CSV export")
    ap.add_argument("tariff_config", help="Path to a JSON file describing the TOU tariff")
    ap.add_argument("--register", default=None,
                    help="Only include this register (e.g. E1 for general usage, E2 for controlled load). "
                         "Default: include all registers found in the file.")
    ap.add_argument("--out-detail", default=None, help="Write per-interval detail to this CSV path")
    ap.add_argument("--out-summary", default=None, help="Write period/monthly summary to this CSV path")
    args = ap.parse_args()

    try:
        records = parse_flat_csv(args.usage_csv)
    except Exception as e:
        print(f"Error parsing usage CSV: {e}", file=sys.stderr)
        sys.exit(1)

    if not records:
        print("No interval records found in the file.", file=sys.stderr)
        sys.exit(1)

    registers_found = sorted(set(r["register"] for r in records))
    if args.register and args.register not in registers_found:
        print(f"Register '{args.register}' not found. Registers in file: {registers_found}", file=sys.stderr)
        sys.exit(1)

    substituted = sum(1 for r in records if r["quality"] != "A")
    if substituted:
        pct = 100 * substituted / len(records)
        print(f"Note: {substituted} of {len(records)} intervals ({pct:.1f}%) are substituted/estimated, not measured.",
              file=sys.stderr)

    tariff = load_tariff(args.tariff_config)
    detail, summary, daily, monthly, supply_charge_total = apply_tariff(
        records, tariff, register_filter=args.register
    )

    dates = sorted(set(r["date"] for r in records))
    print(f"Parsed {len(records)} intervals spanning {dates[0]} to {dates[-1]} ({len(daily)} days with data).")
    print(f"Registers found: {registers_found}")
    if args.register:
        print(f"Filtered to register: {args.register}")

    print_console_summary_with_plan(tariff, summary, monthly, supply_charge_total)

    if args.out_detail:
        write_detail_csv(args.out_detail, detail)
        print(f"Wrote per-interval detail to {args.out_detail}")

    if args.out_summary:
        write_summary_csv_with_plan(args.out_summary, tariff, summary, monthly, supply_charge_total)
        print(f"Wrote summary to {args.out_summary}")


if __name__ == "__main__":
    main()
