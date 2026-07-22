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

## 3. Run it

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
