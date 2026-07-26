#!/usr/bin/env python3
import csv
import glob
import io
import os
import re
import sys
import uuid
import zipfile

import yaml
from flask import Flask, render_template, request, send_file, jsonify

import flat_usage_tou_calculator as calc

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB max upload

JOBS_DIR = os.environ.get("JOBS_DIR", "/tmp/flatusage-jobs")


def clean_zip_name(filename):
    """
    Derive a zip name from the uploaded config filename, stripping out
    words like 'config' or 'configuration' (case-insensitive).
    E.g. 'tariff_config_agl.yaml' -> 'tariff_agl.zip'
    """
    base = os.path.splitext(os.path.basename(filename))[0]
    base = re.sub(r"[_\-\s.]*(config|configs|configuration)[_\-\s.]*", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[_\-\s.]+", "_", base).strip("_")
    if not base:
        base = "results"
    return f"{base}.zip"


def parse_summary_totals(path):
    """Read a summary CSV and return the numeric totals section."""
    totals = {}
    with open(path, newline="") as f:
        reader = csv.reader(f)
        in_totals = False
        for row in reader:
            if len(row) >= 1 and row[0] == "-- Totals --":
                in_totals = True
                continue
            if in_totals and len(row) >= 2:
                key, raw = row[0], row[1].replace(",", "")
                if key in (
                    "total_kwh",
                    "total_usage_cost_dollars",
                    "total_supply_charge_dollars",
                    "grand_total_dollars",
                ):
                    try:
                        totals[key] = float(raw)
                    except ValueError:
                        pass
    return totals if totals else None


def build_comparison(result_a, result_b):
    """Build a side-by-side comparison of matching summary files."""
    rows = []
    for ta in result_a["summary_totals"]:
        for tb in result_b["summary_totals"]:
            if ta["summary_file"] == tb["summary_file"]:
                register = (
                    ta["summary_file"]
                    .replace("summary", "")
                    .replace(".csv", "")
                    .strip("_")
                    or "Total"
                )
                cost_a = ta.get("grand_total_dollars", 0)
                cost_b = tb.get("grand_total_dollars", 0)
                diff = round(cost_b - cost_a, 2)
                rows.append(
                    {
                        "register": register,
                        "plan_a": result_a["plan_name"],
                        "plan_b": result_b["plan_name"],
                        "kwh": ta.get("total_kwh"),
                        "cost_a": cost_a,
                        "cost_b": cost_b,
                        "diff": diff,
                        "cheaper": (
                            "b"
                            if cost_b < cost_a
                            else "a" if cost_a < cost_b else "same"
                        ),
                    }
                )
    return rows


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/parse-csv", methods=["POST"])
def parse_csv():
    """Return the date range covered by the uploaded usage CSV."""
    file = request.files.get("usage_csv")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        text = io.StringIO(file.stream.read().decode("utf-8-sig"))
        reader = csv.DictReader(text)
        if "StartDate" not in (reader.fieldnames or []):
            return (
                jsonify(
                    {
                        "error": f"Missing required column: StartDate. Found: {reader.fieldnames}"
                    }
                ),
                400,
            )

        dates = set()
        for row in reader:
            if row.get("StartDate"):
                dt = calc._parse_timestamp(row["StartDate"])
                dates.add(dt.date())

        if not dates:
            return jsonify({"error": "No valid dates found in CSV"}), 400

        return jsonify(
            {
                "start": min(dates).isoformat(),
                "end": max(dates).isoformat(),
                "days": len(dates),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/parse-config", methods=["POST"])
def parse_config():
    """Return the plan_name (and registers if combined) from an uploaded YAML."""
    file = request.files.get("tariff_yaml")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        content = file.stream.read().decode("utf-8")
        data = yaml.safe_load(content) or {}
        plan_name = data.get("plan_name", "(unnamed plan)")
        registers = []
        if isinstance(data.get("registers"), dict):
            registers = sorted(data["registers"].keys())
        return jsonify({"plan_name": plan_name, "registers": registers})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/calculate", methods=["POST"])
def calculate():
    usage_file = request.files.get("usage_csv")
    tariff_file = request.files.get("tariff_yaml")
    tariff_2_file = request.files.get("tariff_yaml_2")
    register = request.form.get("register", "").strip() or None

    if not usage_file or not tariff_file:
        return jsonify(
            {"error": "Both a usage CSV and a tariff YAML are required."}
        ), 400

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    usage_path = os.path.join(job_dir, "usage.csv")
    tariff_path = os.path.join(job_dir, "tariff.yaml")
    usage_file.save(usage_path)
    tariff_file.save(tariff_path)

    configs = [(tariff_path, "config1", tariff_file.filename)]
    if tariff_2_file:
        tariff_2_path = os.path.join(job_dir, "tariff2.yaml")
        tariff_2_file.save(tariff_2_path)
        configs.append((tariff_2_path, "config2", tariff_2_file.filename))

    configs_named = []
    for path, label, original_name in configs:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        plan_name = data.get("plan_name", "(unnamed plan)")
        configs_named.append((path, label, original_name, plan_name))

    results = []
    for path, label, original_name, plan_name in configs_named:
        output_dir = os.path.join(job_dir, label, "output")
        os.makedirs(output_dir, exist_ok=True)

        old_stdout = sys.stdout
        sys.stdout = captured = io.StringIO()
        try:
            calc.run_calculation(usage_path, path, output_dir, register)
        except Exception as e:
            sys.stdout = old_stdout
            return jsonify(
                {
                    "error": f"{label} ({plan_name}): {str(e)}",
                    "output": captured.getvalue(),
                }
            ), 500
        finally:
            sys.stdout = old_stdout

        result_files = sorted(glob.glob(os.path.join(output_dir, "*.csv")))
        file_entries = []
        summary_totals = []
        for fpath in result_files:
            fname = os.path.basename(fpath)
            file_entries.append(
                {
                    "filename": fname,
                    "download_url": f"/download/{job_id}/{label}/{fname}",
                }
            )
            if fname.startswith("summary"):
                totals = parse_summary_totals(fpath)
                if totals:
                    summary_totals.append({**totals, "summary_file": fname})

        zip_name = clean_zip_name(original_name)
        results.append(
            {
                "label": label,
                "config_file": original_name,
                "plan_name": plan_name,
                "zip_name": zip_name,
                "zip_url": f"/download-zip/{job_id}/{label}?download_name={zip_name}",
                "output": captured.getvalue(),
                "files": file_entries,
                "summary_totals": summary_totals,
            }
        )

    comparison = []
    comparison_note = ""
    if len(results) == 2:
        comparison = build_comparison(results[0], results[1])
        if not comparison:
            comparison_note = "Comparison could not be built: the two configs produced different register outputs."

    return jsonify(
        {
            "job_id": job_id,
            "configs": results,
            "comparison": comparison,
            "comparison_note": comparison_note,
        }
    )


@app.route("/download/<job_id>/<config_label>/<filename>")
def download(job_id, config_label, filename):
    filename = os.path.basename(filename)
    file_path = os.path.join(JOBS_DIR, job_id, config_label, "output", filename)
    if not os.path.exists(file_path):
        return "File not found", 404
    return send_file(file_path, as_attachment=True, download_name=filename)


@app.route("/download-zip/<job_id>/<config_label>")
def download_zip(job_id, config_label):
    config_label = os.path.basename(config_label)
    config_dir = os.path.join(JOBS_DIR, job_id, config_label, "output")
    if not os.path.exists(config_dir):
        return "Not found", 404

    files = sorted(glob.glob(os.path.join(config_dir, "*.csv")))
    if not files:
        return "No results available", 404

    zip_path = os.path.join(JOBS_DIR, job_id, f"{config_label}_bundle.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in files:
            zf.write(fpath, os.path.basename(fpath))

    download_name = request.args.get("download_name") or f"{config_label}.zip"
    return send_file(zip_path, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
