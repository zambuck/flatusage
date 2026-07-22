# flatusage TOU Cost Calculator
Calculate cost of an electricity plan based on flat smart meter data

Parses a smart meter flat data file in csv format and applies a Time-of-Use
tariff you define, so you can see what your historical usage would have
cost under that tariff (or compare tariffs by running twice with different
config files).

## 1. Get your flat file

If you're with AGL and have a smart meter you'll likely find an option on
the AGL billing page to download your usage. That should give you a file something
like MyUsageData_21-05-2026.csv with data in the following format:

AccountNumber,NMI,DeviceNumber,DeviceType,RegisterCode,RateTypeDescription,StartDate,EndDate,ProfileReadValue,RegisterReadValue,QualityFlag

## 2. Work out your register numbers

If your existing plan has a controlled load or some other metered usage you may need to separate them out and report on them separately.
In this example the RegisterType ends with E1 for General Usage records, and E2 for Controlled Load records.

AccountNumber,NMI,DeviceNumber,DeviceType,RegisterCode,RateTypeDescription,StartDate,EndDate,ProfileReadValue,RegisterReadValue,QualityFlag

1231231231,45645645645,000000000789789789,COMMS4D,12345#**E1**,Generalusage,21/05/2024 12:00:00 AM,21/05/2024 12:29:59 AM,0.3982,0,A

1231231231,45645645645,000000000789789789,COMMS4D,12345#**E2**,Controlledload,21/05/2024 12:00:00 AM,21/05/2024 12:29:59 AM,0,0,A

## 3. Edit the tariff config

Copy `sample_tariff_config.json` and edit it to match your actual tariff
sheet (from your retailer's Basic Plan Information Document, or your
network's TOU tariff structure). Key things to set:

- `periods`: each has a `name`, optional `days` (`all`/`weekday`/`weekend`),
  optional `months` (e.g. `[12,1,2]` for summer-only pricing), one or more
  time `windows`, and the `rate` in $/kWh.
- `default_rate`: fallback if a time doesn't match any period (belt and
  braces — shouldn't normally trigger if your windows cover 24 hours).
- `daily_supply_charge_dollars`: optional, added once per day present in
  the data.

Periods are checked in order and the first matching window wins, so put
more specific rules (e.g. a public-holiday or summer-only period) before
general ones if you add them.

## 4. Run it

```bash
python3 flat_usage_tou_calculator.py your_usage.csv your_tariff_config.json \
    --register E1 \
    --out-detail detail.csv \
    --out-summary summary.csv
```

- `--register` — filter to one data stream. Common ones: `E1` (general
  import/consumption), `B1` (solar export), `E2`/`B2` (controlled load or
  second element). Leave it off to include everything in the file at once
  (only useful if you don't have solar/multiple registers, otherwise
  they'll be summed together which isn't meaningful).
- `--out-detail` — optional per-30-minute-interval CSV with the tariff
  period and cost applied to every reading. Good for spot-checking or
  building your own pivot tables.
- `--out-summary` — cost broken down by TOU period and by month, plus
  totals.

Console output gives you the same breakdown without needing to open a
file.

## 5. Compare costs

The output starts with a summary of the plan configuration.

```
=== Plan: AGL **NEW** Night Saver EV Actual E1 TOU tariff ===

=== TOU rate structure ===
  ev            00:00-06:00                   (all)   0.0800 $/kWh
  peak          16:00-20:00                   (weekday)   0.4242 $/kWh
  solar_soak    10:00-14:00                   (all)   0.1111 $/kWh
  default       (any time not matched above)  (all)   0.3212 $/kWh
  supply charge  per day                                          1.6662 $/day
```

Then a summary of the actual usage separated into the separate ToU categories.
```
=== TOU cost breakdown (actual usage) ===
  default          9291.44 kWh   $  2984.51   (avg 0.3212 $/kWh)
  ev               8897.84 kWh   $   711.82   (avg 0.0800 $/kWh)
  peak             2950.95 kWh   $  1251.90   (avg 0.4242 $/kWh)
  solar_soak       3089.31 kWh   $   343.32   (avg 0.1111 $/kWh)
```

Monthly total usage and rated cost.
```
=== Monthly totals ===
  2024-05       244.88 kWh   $    75.71
  2024-06      1002.27 kWh   $   289.67
  2024-07      1213.69 kWh   $   346.98
  2024-08      1064.86 kWh   $   287.29
  2024-09      1022.43 kWh   $   267.29
  2024-10       978.07 kWh   $   261.57
  2024-11      1018.16 kWh   $   272.70
  2024-12      1183.76 kWh   $   311.75
  2025-01      1060.59 kWh   $   293.97
  2025-02      1044.87 kWh   $   282.88
  2025-03      1118.40 kWh   $   293.73
  2025-04      1006.62 kWh   $   271.74
  2025-05      1098.64 kWh   $   283.61
  2025-06      1214.62 kWh   $   312.26
  2025-07      1164.85 kWh   $   297.73
  2025-08      1033.86 kWh   $   278.62
  2025-09       926.09 kWh   $   244.68
  2025-10       905.24 kWh   $   245.90
  2025-11       839.05 kWh   $   228.30
  2025-12       963.05 kWh   $   253.26
  2026-01       792.38 kWh   $   227.25
  2026-02       757.44 kWh   $   207.41
  2026-03      1162.68 kWh   $   289.92
  2026-04       760.12 kWh   $   214.78
  2026-05       652.97 kWh   $   168.87
```
The most important section with total comparable costs.
```
=== Totals ===
  Usage:          24229.55 kWh
  Usage cost:     $5291.56
  Supply charge:  $1216.33
  Grand total:    $6507.89
```

## Notes

- The script will warn you if a meaningful chunk of your data is flagged
  "substituted" (estimated) rather than "actual" — worth knowing before
  you draw conclusions from it.
- To compare tariffs (e.g. "would I be better off on my network's other
  TOU offer, or on flat rate"), just make a second tariff config and run
  the script again with `--out-summary` pointing at a different file, then
  diff the totals.
- This only calculates usage + supply charge costs from your tariff
  config — it doesn't include solar feed-in credits, discounts, GST
  treatment, or other bill line items. Treat it as a like-for-like usage
  cost comparison tool, not a full bill replica.
- If you are actively comparing fixed-rate vs time-of-use plans you should always
  remember that you have a degree of control over some load such as washing machines
  and dishwashers so your future usage may be different based on a new ToU plan.

## Redact Usage Data

If there's ever a reason to share or upload your usage data you'll want to redact the sensitive fields before it leaves your hands.

How it works:

Redacts by default: AccountNumber, NMI, DeviceNumber — the fields that identify a customer or premises. Everything else (device type, register code, rate type, timestamps, usage readings, quality flag) stays untouched since that's just the consumption data.

Two modes:

--mode hash (default): replaces each value with a deterministic pseudonym like ACCO_191760fa28. Same real value → same pseudonym, so you can still tell which rows belong to the same account/meter without exposing the real numbers. Use --salt yoursalt to get reproducible output across runs (otherwise a random salt is used and printed to stderr each time).

--mode mask: keeps the last 4 characters and blanks the rest with X, e.g. XXXXXX1231.

Usage:
```
python redact_usage_data.py input.csv output.csv
python redact_usage_data.py input.csv output.csv --mode mask
python redact_usage_data.py input.csv output.csv --fields AccountNumber NMI
```
You can pass --fields to redact a different set of columns if a future export from the retailer uses different header names or you want to redact more/fewer fields.
