#!/usr/bin/env python3
"""
redact_usage_data.py

Redacts sensitive identifying fields from electricity retailer interval-usage
CSV exports (AccountNumber, NMI, DeviceNumber by default), while preserving
row-to-row consistency (the same real value always redacts to the same
placeholder) so the data remains usable for analysis/testing.

Usage:
    python redact_usage_data.py input.csv output.csv
    python redact_usage_data.py input.csv output.csv --fields AccountNumber NMI DeviceNumber
    python redact_usage_data.py input.csv output.csv --mode mask
    python redact_usage_data.py input.csv output.csv --mode hash --salt mysecretsalt

Modes:
    hash (default) - deterministic pseudonymised value, e.g. "ACCT_9f3a1c2b"
                      (salted SHA-256, truncated). Same real value -> same
                      output within a run, but not reversible without the salt.
    mask            - keeps the field length and trailing 4 characters,
                       replaces the rest with 'X', e.g. "XXXXXX3231"

Default sensitive fields (case-insensitive match on header name):
    AccountNumber, NMI, DeviceNumber
"""

import argparse
import csv
import hashlib
import secrets
import sys

DEFAULT_SENSITIVE_FIELDS = ["AccountNumber", "NMI", "DeviceNumber"]


def make_hash_redactor(salt: str):
    cache = {}

    def redact(field_name: str, value: str) -> str:
        if value == "":
            return value
        key = (field_name, value)
        if key not in cache:
            digest = hashlib.sha256((salt + field_name + value).encode("utf-8")).hexdigest()[:10]
            prefix = "".join(ch for ch in field_name.upper() if ch.isalpha())[:4] or "VAL"
            cache[key] = f"{prefix}_{digest}"
        return cache[key]

    return redact


def make_mask_redactor(keep_last: int = 4):
    def redact(field_name: str, value: str) -> str:
        if value == "":
            return value
        if len(value) <= keep_last:
            return "X" * len(value)
        return "X" * (len(value) - keep_last) + value[-keep_last:]

    return redact


def redact_file(input_path: str, output_path: str, fields_to_redact, mode: str, salt: str):
    fields_lower = {f.lower() for f in fields_to_redact}

    if mode == "hash":
        redactor = make_hash_redactor(salt)
    elif mode == "mask":
        redactor = make_mask_redactor()
    else:
        raise ValueError(f"Unknown mode: {mode}")

    with open(input_path, "r", newline="", encoding="utf-8-sig") as infile:
        reader = csv.DictReader(infile)
        if reader.fieldnames is None:
            raise ValueError("Input file has no header row / appears empty.")

        matched = [f for f in reader.fieldnames if f.lower() in fields_lower]
        missing = fields_lower - {f.lower() for f in matched}
        if missing:
            print(
                f"Warning: requested field(s) not found in CSV header and will be skipped: "
                f"{', '.join(sorted(missing))}",
                file=sys.stderr,
            )

        rows = []
        for row in reader:
            for field in matched:
                row[field] = redactor(field, row[field])
            rows.append(row)

    with open(output_path, "w", newline="", encoding="utf-8") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Redacted {len(rows)} row(s). Fields redacted: {', '.join(matched) if matched else '(none)'}")
    print(f"Output written to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Redact sensitive fields from electricity usage CSV files.")
    parser.add_argument("input_csv", help="Path to the source CSV file.")
    parser.add_argument("output_csv", help="Path to write the redacted CSV file.")
    parser.add_argument(
        "--fields",
        nargs="+",
        default=DEFAULT_SENSITIVE_FIELDS,
        help=f"Column names to redact (default: {' '.join(DEFAULT_SENSITIVE_FIELDS)})",
    )
    parser.add_argument(
        "--mode",
        choices=["hash", "mask"],
        default="hash",
        help="Redaction mode: 'hash' (deterministic pseudonym) or 'mask' (keep last 4 chars). Default: hash.",
    )
    parser.add_argument(
        "--salt",
        default=None,
        help="Salt for hash mode. If omitted, a random salt is generated each run "
        "(so re-running will NOT reproduce the same hashes unless you supply a fixed --salt).",
    )
    args = parser.parse_args()

    salt = args.salt if args.salt is not None else secrets.token_hex(8)
    if args.mode == "hash" and args.salt is None:
        print(
            f"No --salt given; using random salt '{salt}' for this run. "
            "Pass --salt to reproduce the same pseudonyms across runs.",
            file=sys.stderr,
        )

    redact_file(args.input_csv, args.output_csv, args.fields, args.mode, salt)


if __name__ == "__main__":
    main()
