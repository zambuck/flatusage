# flatusage TOU Cost Calculator

Calculate cost of an electricity plan based on flat smart-meter interval data.

Parses a retailer-portal "MyUsageData" CSV export and applies a Time-of-Use
(TOU) tariff you define in a YAML config, so you can see what your historical
usage would have cost under that tariff. You can model a single register such
as E1, or define a combined tariff with separate rates for General Usage
(E1) and Controlled Load (E2).

## 1. Install the dependency

The calculator needs PyYAML. Install it with:

```bash
python3 -m pip install -r requirements.txt
```

## 2. Get your flat file
If you're with AGL and have a smart meter you'll likely find an option on the AGL billing page to download your usage. That should give you a file like MyUsageData_21-05-2026.csv with columns such as:

```
AccountNumber,NMI,DeviceNumber,DeviceType,RegisterCode,RateTypeDescription,StartDate,EndDate,ProfileReadValue,RegisterReadValue,QualityFlag
```

## 3. Work out your register numbers

If your existing plan has a controlled load or some other metered usage you may need to separate them out and report on them separately.
In this example the RegisterType ends with E1 for General Usage records, and E2 for Controlled Load records. Note: The double-asterisks are to highlight the register numbers.

AccountNumber,NMI,DeviceNumber,DeviceType,RegisterCode,RateTypeDescription,StartDate,EndDate,ProfileReadValue,RegisterReadValue,QualityFlag

1231231231,45645645645,000000000789789789,COMMS4D,12345#**E1**,Generalusage,21/05/2024 12:00:00 AM,21/05/2024 12:29:59 AM,0.3982,0,A

1231231231,45645645645,000000000789789789,COMMS4D,12345#**E2**,Controlledload,21/05/2024 12:00:00 AM,21/05/2024 12:29:59 AM,0,0,A

Common registers:

E1 — General import/consumption.

E2 / B2 — Controlled load or second element.

B1 — Solar export.

## 4. Edit the tariff config
Configs are now YAML files. Copy one of the samples and edit it to match your actual tariff sheet (from your retailer's Basic Plan Information Document, or your network's TOU tariff structure).

### Single-register config

Use this when you only want to model one register (e.g. just E1):

```plan_name: "AGL NEW Night Saver EV Actual E1 TOU tariff"
daily_supply_charge_dollars: 1.666203

periods:
  - name: high_season_peak
    months: [Nov, Dec, Jan, Feb, Mar]
    days: weekday
    windows:
      - { start: "4pm", end: "8pm" }
    rate: 0.419144

  - name: low_season_peak
    months: [Apr, May, Jun, Jul, Aug, Sep, Oct]
    days: weekday
    windows:
      - { start: "4pm", end: "8pm" }
    rate: 0.424237

  - name: solar_soak
    months: [all]
    days: all
    windows:
      - { start: "10am", end: "2pm" }
    rate: 0.111133

  - name: EV
    months: [all]
    days: all
    windows:
      - { start: "12am", end: "6am" }
    rate: 0.0799997

default_rate: 0.321211
```

### Combined E1/E2 config

Use this when your plan has different rates for General Usage and Controlled Load. The script will produce separate E1 and E2 results automatically:
```
plan_name: "AGL NEW Night Saver EV Combined E1/E2 Plan"

registers:
  E1:
    label: "General Usage"
    daily_supply_charge_dollars: 1.666203
    periods:
      - name: high_season_peak
        months: [Nov, Dec, Jan, Feb, Mar]
        days: weekday
        windows:
          - { start: "4pm", end: "8pm" }
        rate: 0.419144

      - name: low_season_peak
        months: [Apr, May, Jun, Jul, Aug, Sep, Oct]
        days: weekday
        windows:
          - { start: "4pm", end: "8pm" }
        rate: 0.424237

      - name: solar_soak
        months: [all]
        days: all
        windows:
          - { start: "10am", end: "2pm" }
        rate: 0.111133

      - name: EV
        months: [all]
        days: all
        windows:
          - { start: "12am", end: "6am" }
        rate: 0.0799997

    default_rate: 0.321211

  E2:
    label: "Controlled Load"
    daily_supply_charge_dollars: 0.12496
    periods: []
    default_rate: 0.18579
```
### Config fields

plan_name — displayed in output.

daily_supply_charge_dollars — optional, added once per day present in the data.

periods — list of tariff periods. Each has: 
  name
  days — optional: all (default), weekday, or weekend.
  months — optional list of months, e.g. [Nov, Dec, Jan, Feb, Mar] or numeric [11, 12, 1, 2, 3], or [all] for every month.
  windows — one or more time ranges, e.g. { start: "4pm", end: "8pm" }. Times can be 12-hour (4pm, 4:30pm, 12am) or 24-hour (16:00).
  rate — $/kWh.
  
default_rate — fallback if a time doesn't match any period.

Periods are checked in order and the first matching window wins, so put more specific rules before general ones.

## 5. Run it

### Single register

```
python3 flat_usage_tou_calculator.py your_usage.csv config/your_tariff_config.yaml \
    --register E1 \
    --out-detail output/detail.csv \
    --out-summary output/summary.csv
```

### Combined config — both E1 and E2 automatically

Leave off --register and the script will process every register defined in the combined config that is also present in your usage file:

```
python3 flat_usage_tou_calculator.py data/sampledata-21-05-2026 config/sample_combined_config.yaml \
    --out-detail output/detail.csv \
    --out-summary output/summary.csv
```

This produces summary_E1.csv, summary_E2.csv, detail_E1.csv, and detail_E2.csv, plus a combined grand total in the console.

### Just one register from a combined config

```
python3 flat_usage_tou_calculator.py your_usage.csv config/combined_tariff_config.yaml \
    --register E1 \
    --out-summary output/summary_e1.csv \
    --out-detail output/detail_e1.csv
```

### Command-line options
--register — filter to one data stream. With a combined config, leaving this off processes all configured registers.

--out-detail — optional per-interval CSV with the tariff period and cost applied to every reading. Good for spot-checking or building your own pivot tables.

--out-summary — cost broken down by TOU period and by month, plus totals. When multiple registers are processed, files are suffixed with _E1, _E2, etc.

Console output gives you the same breakdown without needing to open a file.

## 6. Compare costs

The output starts with a summary of the plan configuration:

```
=== Plan: AGL NEW Night Saver EV Actual E1 TOU tariff — General Usage (E1) ===

=== TOU rate structure ===
  high_season_peak  4pm-8pm                       (weekday, months Nov,Dec,Jan,Feb,Mar)   0.4191 $/kWh
  low_season_peak   4pm-8pm                       (weekday, months Apr,May,Jun,Jul,Aug,Sep,Oct)   0.4242 $/kWh
  solar_soak        10am-2pm                      (all)   0.1111 $/kWh
  EV                12am-6am                      (all)   0.0800 $/kWh
  default           (any time not matched above)  (all)   0.3212 $/kWh
  supply charge     per day                                          1.6662 $/day
```

Then a summary of the actual usage separated into the separate TOU categories:

```
=== TOU cost breakdown (actual usage) ===
  default          9291.44 kWh   $  2984.51   (avg 0.3212 $/kWh)
  EV               8897.84 kWh   $   711.82   (avg 0.0800 $/kWh)
  high_season_peak 2950.95 kWh   $  1236.12   (avg 0.4186 $/kWh)
  solar_soak       3089.31 kWh   $   343.32   (avg 0.1111 $/kWh)
```

Monthly total usage and rated cost:

```
=== Monthly totals ===
  2024-05       244.88 kWh   $    75.71
  2024-06      1002.27 kWh   $   289.67
  ...
```

The most important section with total comparable costs:

```
=== Totals ===
  Usage:          24229.55 kWh
  Usage cost:     $5291.56
  Supply charge:  $1216.33
  Grand total:    $6507.89
```

With a combined config, a combined grand total across all processed registers is printed at the end.

## Notes
The script will warn you if a meaningful chunk of your data is flagged "substituted" (estimated) rather than "actual" — worth knowing before you draw conclusions from it.

To compare tariffs (e.g. "would I be better off on my network's other TOU offer, or on flat rate"), just make a second tariff config and run the script again with --out-summary pointing at a different file, then diff the totals.

This only calculates usage + supply charge costs from your tariff config — it doesn't include solar feed-in credits, discounts, GST treatment, or other bill line items. Treat it as a like-for-like usage cost comparison tool, not a full bill replica.

If you are actively comparing fixed-rate vs time-of-use plans you should always remember that you have a degree of control over some load such as washing machines and dishwashers, so your future usage may be different based on a new TOU plan.

## Redact Usage Data

If there's ever a reason to share or upload your usage data you'll want to redact the sensitive fields before it leaves your hands.

How it works:

Redacts by default: AccountNumber, NMI, DeviceNumber — the fields that identify a customer or premises. Everything else (device type, register code, rate type, timestamps, usage readings, quality flag) stays untouched since that's just the consumption data.

Two modes:

--mode hash (default): replaces each value with a deterministic pseudonym like ACCO_191760fa28. Same real value → same pseudonym, so you can still tell which rows belong to the same account/meter without exposing the real numbers. Use --salt yoursalt to get reproducible output across runs (otherwise a random salt is used and printed to stderr each time).

--mode mask: keeps the last 4 characters and blanks the rest with X, e.g. XXXXXX1231.

Usage:
```
python3 redact_data_usage.py input.csv output.csv
python3 redact_data_usage.py input.csv output.csv --mode mask
python3 redact_data_usage.py input.csv output.csv --fields AccountNumber NMI
```
You can pass --fields to redact a different set of columns if a future export from the retailer uses different header names or you want to redact more/fewer fields.
