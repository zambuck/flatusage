#!/usr/bin/env python3
"""
flat_usage_tou_calculator.py

Parses the retailer-portal "MyUsageData" flat CSV export (one row per
30-minute interval per register) and applies a Time-of-Use (TOU) tariff
defined in a YAML config, so you can see what your historical usage would
have cost under that tariff.

Expected CSV columns include:

    AccountNumber,NMI,DeviceNumber,DeviceType,RegisterCode,
    RateTypeDescription,StartDate,EndDate,ProfileReadValue,
    RegisterReadValue,QualityFlag

Usage
-----
    python3 flat_usage_tou_calculator.py MyUsageData.csv tariff_config.yaml \
        --register E1 \
        --out-summary summary.csv \
        --out-detail detail.csv
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from datetime import datetime, date, time as dtime

import yaml

# ---------------------------------------------------------------------------
# TARIFF ENGINE
# ---------------------------------------------------------------------------

MONTH_NAME_TO_NUM = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def load_tariff(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_hhmm(s):
    """
    Parse a time string into a datetime.time.

    Accepts 12-hour AM/PM forms such as:
        "4pm", "4:30pm", "4.30pm", "4:30 pm", "12am", "12pm"
    and 24-hour forms such as:
        "16:00", "16"
    """
    raw = s.strip().lower().replace(" ", "")
    # 12-hour with optional minutes and am/pm
    m = re.match(r"(\d{1,2})(?::(\d{2}))?(?:\.(\d{2}))?(am|pm)$", raw)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or m.group(3) or "0")
        suffix = m.group(4)
        if hour == 12:
            hour = 0 if suffix == "am" else 12
        elif suffix == "pm":
            hour += 12
        return dtime(hour, minute)
    # 24-hour with optional minutes
    m = re.match(r"(\d{1,2})(?::(\d{2}))?$", raw)
    if m:
        return dtime(int(m.group(1)), int(m.group(2) or "0"))
    raise ValueError(f"Cannot parse time: {s!r}")


def in_window(t, start_s, end_s):
    """True if time t falls in [start, end). Handles wrap-around windows."""
    start = parse_hhmm(start_s)
    end = parse_hhmm(end_s)
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _parse_months(months):
    """
    Convert a months spec into a list of integers, or None for "all months".

    Accepts names/abbreviations, numeric strings/integers, or the string "all".
    """
    if months is None:
        return None
    if isinstance(months, str):
        months = [months]
    if not isinstance(months, list):
        raise ValueError(f"months must be a list, got {type(months).__name__}")

    parsed = []
    for item in months:
        s = str(item).strip().lower()
        if s == "all":
            return None
        if s.isdigit():
            parsed.append(int(s))
        else:
            num = MONTH_NAME_TO_NUM.get(s)
            if num is None:
                raise ValueError(f"Unknown month: {item!r}")
            parsed.append(num)
    return parsed


def classify_interval(d: date, t: dtime, tariff):
    """
    Return the tariff period name and rate for a given date+time.
    Rules are checked in order; first match wins.
    """
    weekday = d.weekday()  # 0=Mon .. 6=Sun
    is_weekend = weekday >= 5
    month = d.month

    for period in tariff.get("periods", []):
        # day-type filter
        day_type = period.get("days", "all")  # all | weekday | weekend
        if day_type == "weekday" and is_weekend:
            continue
        if day_type == "weekend" and not is_weekend:
            continue

        # month/season filter
        months = _parse_months(period.get("months"))
        if months is not None and month not in months:
            continue

        for window in period["windows"]:
            if in_window(t, window["start"], window["end"]):
                return period["name"], period["rate"]

    if "default_rate" in tariff:
        return "default", tariff["default_rate"]

    raise ValueError(
        f"No tariff period matched {d} {t} and no 'default_rate' set in config."
    )


def apply_tariff(records, tariff, register_filter=None):
    """
    Returns:
      detail: list of records with added 'period', 'rate', and 'cost' fields
      summary: dict of period -> {kwh, cost, intervals}
      daily: dict of date -> {kwh, cost}
      monthly: dict of 'YYYY-MM' -> {kwh, cost}
      supply_charge_total: float
    """
    detail = []
    summary = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0, "intervals": 0})
    daily = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0})
    monthly = defaultdict(lambda: {"kwh": 0.0, "cost": 0.0})

    for r in records:
        if register_filter and r["register"] != register_filter:
            continue

        period_name, rate = classify_interval(r["date"], r["time"], tariff)
        cost = r["kwh"] * rate

        row = dict(r)
        row["period"] = period_name
        row["rate"] = rate
        row["cost"] = cost
        detail.append(row)

        summary[period_name]["kwh"] += r["kwh"]
        summary[period_name]["cost"] += cost
        summary[period_name]["intervals"] += 1

        daily[r["date"]]["kwh"] += r["kwh"]
        daily[r["date"]]["cost"] += cost

        month_key = f"{r['date'].year:04d}-{r['date'].month:02d}"
        monthly[month_key]["kwh"] += r["kwh"]
        monthly[month_key]["cost"] += cost

    supply_charge_total = 0.0
    if tariff.get("daily_supply_charge_dollars"):
        charge = tariff["daily_supply_charge_dollars"]
        n_days = len(daily)
        supply_charge_total = charge * n_days
        for d_key in daily:
            daily[d_key]["cost"] += charge
        for m_key in monthly:
            n_days_in_month = sum(
                1 for d in daily if f"{d.year:04d}-{d.month:02d}" == m_key
            )
            monthly[m_key]["cost"] += charge * n_days_in_month

    return detail, summary, daily, monthly, supply_charge_total


# ---------------------------------------------------------------------------
# FLAT CSV PARSING
# ---------------------------------------------------------------------------

def _parse_timestamp(s):
    """Try a few common retailer timestamp formats."""
    s = s.strip()
    formats = [
        "%d/%m/%Y %I:%M:%S %p",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse timestamp: {s!r}")


def parse_flat_csv(path):
    """
    Parse the flat retailer usage-export CSV into the same record shape
    used by the tariff engine: {nmi, register, date, time, kwh, quality}
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
            raw_register = row["RegisterCode"].strip()
            register = raw_register.split("#")[-1] if "#" in raw_register else raw_register

            start = _parse_timestamp(row["StartDate"])

            kwh_str = row["ProfileReadValue"].strip()
            if kwh_str == "":
                continue
            kwh = float(kwh_str)

            q = row["QualityFlag"].strip().upper()
            quality = q[0] if q else "A"

            records.append({
                "nmi": row["NMI"].strip(),
                "register": register,
                "date": start.date(),
                "time": start.time(),
                "kwh": kwh,
                "quality": quality,
            })
    return records


# ---------------------------------------------------------------------------
# OUTPUT HELPERS
# ---------------------------------------------------------------------------

def _month_name(n):
    return ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][n]


def describe_periods(tariff):
    """
    Build a human-readable list of the TOU periods defined in the tariff
    config for display in the summary output.
    """
    rows = []
    for period in tariff.get("periods", []):
        days = period.get("days", "all")
        months = _parse_months(period.get("months"))
        months_str = ",".join(_month_name(m) for m in months) if months else ""
        windows_str = ", ".join(f"{w['start']}-{w['end']}" for w in period["windows"])
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


def write_detail_csv(path, detail):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["nmi", "register", "date", "time", "kwh", "quality", "period", "rate", "cost"])
        for r in detail:
            w.writerow([
                r["nmi"], r["register"], r["date"].isoformat(), r["time"].strftime("%H:%M"),
                f"{r['kwh']:.4f}", r["quality"], r["period"], f"{r['rate']:.4f}", f"{r['cost']:.4f}"
            ])


def write_summary_csv(path, tariff, summary, monthly, supply_charge_total):
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


def print_console_summary(tariff, summary, monthly, supply_charge_total):
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


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("usage_csv", help="Path to your flat 'MyUsageData' CSV export")
    ap.add_argument("tariff_config", help="Path to a YAML file describing the TOU tariff")
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

    try:
        tariff = load_tariff(args.tariff_config)
    except Exception as e:
        print(f"Error loading tariff config: {e}", file=sys.stderr)
        sys.exit(1)

    detail, summary, daily, monthly, supply_charge_total = apply_tariff(
        records, tariff, register_filter=args.register
    )

    substituted = sum(1 for r in detail if r["quality"] != "A")
    if substituted:
        pct = 100 * substituted / len(detail)
        print(f"Note: {substituted} of {len(detail)} intervals ({pct:.1f}%) are substituted/estimated, not measured.",
              file=sys.stderr)

    filtered_dates = sorted({r["date"] for r in detail})
    print(f"Parsed {len(records)} total intervals; {len(detail)} used after filtering.")
    print(f"Data spans {filtered_dates[0]} to {filtered_dates[-1]} ({len(daily)} days with data).")
    print(f"Registers found in file: {registers_found}")
    if args.register:
        print(f"Filtered to register: {args.register}")

    print_console_summary(tariff, summary, monthly, supply_charge_total)

    if args.out_detail:
        write_detail_csv(args.out_detail, detail)
        print(f"Wrote per-interval detail to {args.out_detail}")

    if args.out_summary:
        write_summary_csv(args.out_summary, tariff, summary, monthly, supply_charge_total)
        print(f"Wrote summary to {args.out_summary}")


if __name__ == "__main__":
    main()
