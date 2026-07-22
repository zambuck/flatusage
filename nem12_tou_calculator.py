#!/usr/bin/env python3
"""
nem12_tou_calculator.py

Parses an Australian NEM12 smart meter interval data file (the standard
format your retailer/DNSP gives you when you request historical usage)
and applies a Time-of-Use (TOU) tariff you define in a config file, so you
can see what your historical usage would have cost under that tariff.

NEM12 background
-----------------
NEM12 is a block-structured CSV, not a plain table:
  100 record - header
  200 record - NMI details (NMI, register/data-stream ID e.g. E1/B1,
               interval length in minutes, next 200 record starts a
               new NMI/register block)
  300 record - one row per DAY: date + all the interval readings for
               that day (e.g. 48 values for 30-minute intervals),
               followed by daily quality method fields
  400 record - quality/substitution override for part of a 300 row
               (optional, only present when some intervals were
               estimated rather than measured)
  900 record - end of file

This script reads the 200/300 records and reshapes them into a tidy
long-format table: one row per (date, time, register, kWh, quality).

Usage
-----
    python3 nem12_tou_calculator.py usage.csv tariff_config.json \
        --register E1 \
        --out-summary summary.csv \
        --out-detail detail.csv

Run with --help for all options.
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, date, time as dtime
from collections import defaultdict


# ---------------------------------------------------------------------------
# NEM12 PARSING
# ---------------------------------------------------------------------------

def parse_nem12(path):
    """
    Parse a NEM12 CSV file into a list of dicts:
        {nmi, register, interval_minutes, date, time, kwh, quality}

    quality is 'A' (actual) unless overridden by a 400 record, in which
    case it's whatever quality method code applies to that interval
    (commonly 'S' for substituted/estimated).
    """
    records = []
    current_nmi = None
    current_register = None
    current_interval_minutes = None
    # buffer of 300 rows for the current NMI/register block, so 400
    # records (which follow the 300 rows they modify) can be applied
    pending_rows = []  # list of dicts, mutable, referenced by index

    def flush_pending():
        records.extend(pending_rows)
        pending_rows.clear()

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            rec_type = row[0].strip()

            if rec_type == "100":
                continue  # header, nothing needed

            elif rec_type == "200":
                # 200,NMI,NMI config,register1,register2,NMI suffix,
                # MDP,datastream suffix,meter serial,uom,interval length,next reading...
                flush_pending()
                current_nmi = row[1].strip()
                current_register = row[4].strip() if len(row) > 4 and row[4].strip() else row[3].strip()
                current_interval_minutes = int(row[8].strip()) if len(row) > 8 and row[8].strip() else 30

            elif rec_type == "300":
                # 300,date,interval values...,quality,method,reason code,reason desc,updateDateTime,msats loadDateTime
                if current_nmi is None:
                    raise ValueError("Found a 300 record before any 200 record - file may be corrupt.")
                day_str = row[1].strip()
                day = datetime.strptime(day_str, "%Y%m%d").date()
                n_intervals = (24 * 60) // current_interval_minutes
                values = row[2:2 + n_intervals]
                # Trailing fields after the interval values: quality method etc.
                # Default quality for the whole day unless a 400 overrides part of it.
                day_quality = row[2 + n_intervals].strip() if len(row) > 2 + n_intervals else "A"

                day_start_idx = len(pending_rows)
                for i, val in enumerate(values):
                    if val == "" or val is None:
                        continue
                    minutes_from_midnight = i * current_interval_minutes
                    t = (datetime.combine(day, dtime(0, 0)) +
                         timedelta(minutes=minutes_from_midnight)).time()
                    pending_rows.append({
                        "nmi": current_nmi,
                        "register": current_register,
                        "date": day,
                        "time": t,
                        "interval_index": i,
                        "kwh": float(val),
                        "quality": day_quality if day_quality in ("A", "S", "F", "N") else "A",
                        "_day_start_idx": day_start_idx,
                    })

            elif rec_type == "400":
                # 400,start interval,end interval,quality method,reason code,reason desc
                # Overrides quality for a range of intervals in the MOST RECENT 300 row
                if not pending_rows:
                    continue
                start_i = int(row[1].strip())
                end_i = int(row[2].strip())
                method = row[3].strip() if len(row) > 3 else "S"
                # find the most recent day's block
                last_day_start = pending_rows[-1]["_day_start_idx"]
                for r in pending_rows[last_day_start:]:
                    if start_i <= (r["interval_index"] + 1) <= end_i:
                        r["quality"] = method

            elif rec_type == "900":
                flush_pending()
                break

    flush_pending()
    for r in records:
        r.pop("_day_start_idx", None)
    return records


# ---------------------------------------------------------------------------
# TOU TARIFF APPLICATION
# ---------------------------------------------------------------------------

def load_tariff(path):
    with open(path) as f:
        return json.load(f)


def parse_hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_window(t, start_s, end_s):
    """True if time t falls in [start, end). Handles windows that don't
    wrap midnight (all NEM12 intervals are within a single day already)."""
    start = parse_hhmm(start_s)
    end = parse_hhmm(end_s)
    if start <= end:
        return start <= t < end
    # wrap-around window, e.g. 22:00-06:00
    return t >= start or t < end


def classify_interval(d: date, t: dtime, tariff):
    """
    Return the tariff period name (e.g. 'peak') for a given date+time,
    based on the tariff config's season/day-type/window rules.
    Rules are checked in order; first match wins. A 'default' period
    can be set to catch anything unmatched.
    """
    weekday = d.weekday()  # 0=Mon .. 6=Sun
    is_weekend = weekday >= 5
    month = d.month

    # Optional EV rate: a separate, always-on override for a designated
    # overnight charging window. If set in the config, it takes priority
    # over the normal periods for that window. If not set, the window
    # falls through to whichever normal period/default would otherwise
    # apply (e.g. off-peak).
    if "ev_rate" in tariff:
        ev_window = tariff.get("ev_window", {"start": "00:00", "end": "06:00"})
        if in_window(t, ev_window["start"], ev_window["end"]):
            return "ev", tariff["ev_rate"]

    for period in tariff["periods"]:
        # optional day-type filter
        day_type = period.get("days", "all")  # all | weekday | weekend
        if day_type == "weekday" and is_weekend:
            continue
        if day_type == "weekend" and not is_weekend:
            continue

        # optional month/season filter
        months = period.get("months")  # e.g. [11,12,1,2,3] for summer
        if months and month not in months:
            continue

        for window in period["windows"]:
            if in_window(t, window["start"], window["end"]):
                return period["name"], period["rate"]

    # nothing matched - use default if present
    if "default_rate" in tariff:
        return "default", tariff["default_rate"]
    raise ValueError(
        f"No tariff period matched {d} {t} and no 'default_rate' set in config."
    )


def apply_tariff(records, tariff, register_filter=None):
    """
    Returns:
      detail: list of records with added 'period' and 'cost' fields
      summary: dict of period -> {kwh, cost, count}
      daily: dict of date -> {kwh, cost}
      monthly: dict of 'YYYY-MM' -> {kwh, cost}
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

    # daily supply charge, if configured (applied once per calendar day present in the data)
    supply_charge_total = 0.0
    if tariff.get("daily_supply_charge_dollars"):
        charge = tariff["daily_supply_charge_dollars"]
        n_days = len(daily)
        supply_charge_total = charge * n_days
        for d_key in daily:
            daily[d_key]["cost"] += charge
        for m_key in monthly:
            # count days in this month present in the data
            n_days_in_month = sum(1 for d in daily if f"{d.year:04d}-{d.month:02d}" == m_key)
            monthly[m_key]["cost"] += charge * n_days_in_month

    return detail, summary, daily, monthly, supply_charge_total


# ---------------------------------------------------------------------------
# OUTPUT HELPERS
# ---------------------------------------------------------------------------

def write_detail_csv(path, detail):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["nmi", "register", "date", "time", "kwh", "quality", "period", "rate", "cost"])
        for r in detail:
            w.writerow([
                r["nmi"], r["register"], r["date"].isoformat(), r["time"].strftime("%H:%M"),
                f"{r['kwh']:.4f}", r["quality"], r["period"], f"{r['rate']:.4f}", f"{r['cost']:.4f}"
            ])


def write_summary_csv(path, summary, monthly, supply_charge_total, tariff):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["-- By TOU period --"])
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


def print_console_summary(summary, monthly, supply_charge_total):
    total_kwh = sum(v["kwh"] for v in summary.values())
    total_cost = sum(v["cost"] for v in summary.values())

    print("\n=== TOU cost breakdown ===")
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
    ap.add_argument("nem12_file", help="Path to your NEM12 CSV file from your retailer/DNSP")
    ap.add_argument("tariff_config", help="Path to a JSON file describing the TOU tariff (see sample_tariff_config.json)")
    ap.add_argument("--register", default=None,
                    help="Only include this register/data-stream (e.g. E1 for general import, B1 for solar export). "
                         "Default: include all registers found in the file.")
    ap.add_argument("--out-detail", default=None, help="Write per-interval detail to this CSV path")
    ap.add_argument("--out-summary", default=None, help="Write period/monthly summary to this CSV path")
    args = ap.parse_args()

    try:
        records = parse_nem12(args.nem12_file)
    except Exception as e:
        print(f"Error parsing NEM12 file: {e}", file=sys.stderr)
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

    print(f"Parsed {len(records)} intervals across {len(daily)} days. Registers found: {registers_found}")
    if args.register:
        print(f"Filtered to register: {args.register}")

    print_console_summary(summary, monthly, supply_charge_total)

    if args.out_detail:
        write_detail_csv(args.out_detail, detail)
        print(f"Wrote per-interval detail to {args.out_detail}")

    if args.out_summary:
        write_summary_csv(args.out_summary, summary, monthly, supply_charge_total, tariff)
        print(f"Wrote summary to {args.out_summary}")


if __name__ == "__main__":
    main()
