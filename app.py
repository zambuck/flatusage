#!/usr/bin/env python3
"""
Production-hardened Flask web front-end for the flatusage TOU calculator.
"""
import csv
import glob
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from collections import defaultdict
from datetime import datetime, date, timedelta
from functools import wraps

import yaml
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
)
from flask_limiter import Limiter
from flask_talisman import Talisman

# ---------------------------------------------------------------------------
# DEBUG CONFIG
# ---------------------------------------------------------------------------

DEBUG = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")
LOG_LEVEL = logging.DEBUG if DEBUG else logging.INFO

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
app.config["PROPAGATE_EXCEPTIONS"] = False

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("flatusage")

# ---------------------------------------------------------------------------
# SECURITY CONFIG
# ---------------------------------------------------------------------------

JOBS_DIR = os.environ.get("JOBS_DIR", "/tmp/flatusage-jobs")
JOB_MAX_AGE_MINUTES = int(os.environ.get("JOB_MAX_AGE_MINUTES", "60"))
CALC_TIMEOUT_SECONDS = int(os.environ.get("CALC_TIMEOUT_SECONDS", "120"))

MAX_YAML_SIZE = 1024 * 1024
MAX_CSV_SIZE = 100 * 1024 * 1024

ALLOWED_EXTENSIONS = {
    "usage_csv": {".csv"},
    "tariff_yaml": {".yaml", ".yml"},
}

SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

ALLOWED_TOP_KEYS = {
    "plan_name",
    "daily_supply_charge_dollars",
    "periods",
    "registers",
    "default_rate",
}
ALLOWED_PERIOD_KEYS = {"name", "months", "days", "windows", "rate"}
ALLOWED_WINDOW_KEYS = {"start", "end"}
ALLOWED_REGISTER_KEYS = {"label", "daily_supply_charge_dollars", "periods", "default_rate"}

CSV_INJECTION_CHARS = ("=", "+", "-", "@", "\t", "\r")

CALCULATOR_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "flat_usage_tou_calculator.py"
)

# ---------------------------------------------------------------------------
# AUTHENTICATION
# ---------------------------------------------------------------------------

APP_USER = os.environ.get("APP_USER")
APP_PASSWORD = os.environ.get("APP_PASSWORD")
AUTH_ENABLED = bool(APP_USER and APP_PASSWORD)

logger.info(
    f"AUTH_ENABLED={AUTH_ENABLED}, "
    f"APP_USER={'set' if APP_USER else 'unset'}, "
    f"APP_PASSWORD={'set' if APP_PASSWORD else 'unset'}, "
    f"DEBUG={DEBUG}"
)


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not AUTH_ENABLED:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.username != APP_USER or auth.password != APP_PASSWORD:
            logger.warning(f"Failed auth from {request.remote_addr}")
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="flatususe"'},
            )
        return f(*args, **kwargs)

    return decorated


# ---------------------------------------------------------------------------
# RATE LIMITING
# ---------------------------------------------------------------------------

limiter = Limiter(
    app=app,
    key_func=lambda: request.remote_addr,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)


# ---------------------------------------------------------------------------
# SECURITY HEADERS
# ---------------------------------------------------------------------------

force_https = os.environ.get("FORCE_HTTPS", "false").lower() in ("1", "true", "yes")
logger.info(f"Talisman force_https={force_https}")

Talisman(
    app,
    force_https=force_https,
    strict_transport_security=force_https,
    content_security_policy={
        "default-src": "'self'",
        "script-src": "'self'",
        "style-src": "'self' 'unsafe-inline'",
    },
)


# ---------------------------------------------------------------------------
# SANITIZATION HELPERS
# ---------------------------------------------------------------------------


def safe_filename(name):
    base = os.path.basename(name)
    if ".." in base or not SAFE_NAME_RE.match(base):
        raise ValueError(f"Invalid filename: {name}")
    return base


def safe_component(value, allow_list=None):
    if ".." in value or "/" in value or "\\" in value:
        raise ValueError(f"Invalid path component: {value}")
    if allow_list is not None and value not in allow_list:
        raise ValueError(f"Invalid value: {value}")
    return value


def validate_uuid(value):
    try:
        uuid.UUID(value)
        return value
    except (ValueError, TypeError):
        raise ValueError(f"Invalid job ID: {value}")


def sanitize_csv_value(value):
    if isinstance(value, str) and value and value[0] in CSV_INJECTION_CHARS:
        return "'" + value
    return value


def validate_file_size(file_storage, max_bytes):
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > max_bytes:
        raise ValueError(f"File too large: {size} bytes (max {max_bytes})")
    return size


def validate_extension(filename, field):
    ext = os.path.splitext(filename)[1].lower()
    allowed = ALLOWED_EXTENSIONS.get(field, set())
    if ext not in allowed:
        raise ValueError(
            f"Invalid file extension for {field}: {ext}. Allowed: {', '.join(allowed)}"
        )
    return ext


def validate_tariff(data):
    if not isinstance(data, dict):
        raise ValueError("Tariff config must be a YAML mapping")

    for key in data:
        if key not in ALLOWED_TOP_KEYS:
            raise ValueError(f"Unexpected top-level key: {key}")

    periods = data.get("periods", [])
    if not isinstance(periods, list):
        raise ValueError("'periods' must be a list")
    for period in periods:
        if not isinstance(period, dict):
            raise ValueError("Each period must be a mapping")
        for key in period:
            if key not in ALLOWED_PERIOD_KEYS:
                raise ValueError(f"Unexpected period key: {key}")
        for window in period.get("windows", []):
            if not isinstance(window, dict):
                raise ValueError("Each window must be a mapping")
            for key in window:
                if key not in ALLOWED_WINDOW_KEYS:
                    raise ValueError(f"Unexpected window key: {key}")

    registers = data.get("registers")
    if registers is not None:
        if not isinstance(registers, dict):
            raise ValueError("'registers' must be a mapping")
        for register_name, register_config in registers.items():
            if not isinstance(register_config, dict):
                raise ValueError(f"Register {register_name} config must be a mapping")
            for key in register_config:
                if key not in ALLOWED_REGISTER_KEYS:
                    raise ValueError(f"Unexpected register key: {key}")


def sanitize_tariff(data):
    data = yaml.safe_load(yaml.safe_dump(data))
    if "plan_name" in data:
        data["plan_name"] = sanitize_csv_value(data["plan_name"])
    for period in data.get("periods", []):
        if "name" in period:
            period["name"] = sanitize_csv_value(period["name"])
    registers = data.get("registers")
    if isinstance(registers, dict):
        for register_config in registers.values():
            if "label" in register_config:
                register_config["label"] = sanitize_csv_value(register_config["label"])
            for period in register_config.get("periods", []):
                if "name" in period:
                    period["name"] = sanitize_csv_value(period["name"])
    return data


# ---------------------------------------------------------------------------
# CSV PARSING
# ---------------------------------------------------------------------------


def parse_csv_date_range(file_storage):
    text = io.StringIO(file_storage.stream.read().decode("utf-8-sig"))
    file_storage.stream.seek(0)
    reader = csv.DictReader(text)
    fieldnames = reader.fieldnames or []
    required = {"StartDate", "ProfileReadValue", "RegisterCode"}
    missing = required - set(fieldnames)
    if missing:
        raise ValueError(f"CSV missing required columns: {', '.join(missing)}")

    dates = set()
    for row in reader:
        if row.get("StartDate"):
            try:
                dt = datetime.strptime(row["StartDate"].strip(), "%d/%m/%Y %I:%M:%S %p")
                dates.add(dt.date())
            except ValueError:
                pass
    if not dates:
        raise ValueError("No valid StartDate values found in CSV")
    return min(dates), max(dates), len(dates)


# ---------------------------------------------------------------------------
# JOB MANAGEMENT
# ---------------------------------------------------------------------------


def cleanup_old_jobs():
    cutoff = datetime.now().timestamp() - (JOB_MAX_AGE_MINUTES * 60)
    if not os.path.isdir(JOBS_DIR):
        return
    count = 0
    for entry in os.scandir(JOBS_DIR):
        if entry.is_dir() and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry.path, ignore_errors=True)
            count += 1
    if DEBUG and count:
        logger.debug(f"Cleaned up {count} old job directories")


def create_job_dir():
    os.makedirs(JOBS_DIR, mode=0o700, exist_ok=True)
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, mode=0o700, exist_ok=False)
    logger.info(f"Created job directory: {job_dir}")
    return job_id, job_dir


# ---------------------------------------------------------------------------
# CALCULATOR EXECUTION
# ---------------------------------------------------------------------------


def run_calculation_sandboxed(usage_csv, tariff_yaml, output_dir, register=None):
    cmd = [
        sys.executable,
        CALCULATOR_SCRIPT,
        usage_csv,
        tariff_yaml,
        "--out-summary", os.path.join(output_dir, "summary.csv"),
        "--out-detail", os.path.join(output_dir, "detail.csv"),
    ]
    if register:
        cmd.extend(["--register", register])

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    logger.info(f"Running calculator: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=CALC_TIMEOUT_SECONDS,
        env=env,
        cwd=os.path.dirname(CALCULATOR_SCRIPT),
    )
    if result.stderr:
        logger.info(f"Calculator stderr: {result.stderr[:2000]}")
    return result.stdout, result.stderr, result.returncode


# ---------------------------------------------------------------------------
# OUTPUT HELPERS
# ---------------------------------------------------------------------------


def parse_summary_totals(path):
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


def parse_detail_load_profile(path):
    """
    Build an hour-of-day x day-of-week average kWh grid, plus the most common
    TOU period active in each hour, from a per-interval detail CSV.
    Returns None if the file is missing, empty, or unreadable.
    """
    sums = [[0.0] * 24 for _ in range(7)]
    counts = [[0] * 24 for _ in range(7)]
    period_hour_counts = [dict() for _ in range(24)]

    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    d = datetime.strptime(row["date"], "%Y-%m-%d").date()
                    hour = int(row["time"].split(":")[0])
                    kwh = float(row["kwh"])
                except (KeyError, ValueError):
                    continue
                weekday = d.weekday()
                sums[weekday][hour] += kwh
                counts[weekday][hour] += 1
                period = row.get("period") or "unknown"
                period_hour_counts[hour][period] = period_hour_counts[hour].get(period, 0) + 1
    except (OSError, csv.Error):
        return None

    grid = []
    has_data = False
    for weekday in range(7):
        row_out = []
        for hour in range(24):
            n = counts[weekday][hour]
            if n:
                has_data = True
                row_out.append(round(sums[weekday][hour] / n * 2, 4))
            else:
                row_out.append(0.0)
        grid.append(row_out)

    if not has_data:
        return None

    period_by_hour = []
    for hour in range(24):
        hour_counts = period_hour_counts[hour]
        period_by_hour.append(max(hour_counts, key=hour_counts.get) if hour_counts else None)

    return {"grid": grid, "period_by_hour": period_by_hour}


def build_load_profiles(result_files):
    """Find detail_*.csv (or detail.csv) files among a job's outputs and build
    a load profile for each register found."""
    profiles = {}
    for fpath in result_files:
        fname = os.path.basename(fpath)
        stem = os.path.splitext(fname)[0]
        if stem != "detail" and not stem.startswith("detail_"):
            continue
        register = stem[len("detail"):].lstrip("_") or "All"
        profile = parse_detail_load_profile(fpath)
        if profile:
            profiles[register] = profile
    return profiles


# ---------------------------------------------------------------------------
# WEEKLY CHART HELPERS
# ---------------------------------------------------------------------------


def register_tariff_for(tariff, register):
    """Return the tariff that applies to a given register."""
    if register and isinstance(tariff.get("registers"), dict) and register in tariff["registers"]:
        t = dict(tariff["registers"][register])
        t.setdefault("periods", [])
        return t
    return tariff


def parse_detail_weekly(path, tariff):
    """
    Aggregate a detail CSV into ISO-calendar weeks.
    Returns (weeks_dict, last_date) or (None, None) on failure.
    """
    weekly = defaultdict(lambda: {"kwh": 0.0, "cost_by_period": defaultdict(float), "dates": set()})
    last_date = None
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    d = datetime.strptime(row["date"], "%Y-%m-%d").date()
                    kwh = float(row["kwh"])
                    cost = float(row["cost"])
                    period = row.get("period") or "unknown"
                except (KeyError, ValueError):
                    continue
                iso = d.isocalendar()
                week_key = (iso.year, iso.week)
                weekly[week_key]["kwh"] += kwh
                weekly[week_key]["cost_by_period"][period] += cost
                weekly[week_key]["dates"].add(d)
                if last_date is None or d > last_date:
                    last_date = d
    except (OSError, csv.Error):
        return None, None

    if not weekly or last_date is None:
        return None, None

    supply_per_day = tariff.get("daily_supply_charge_dollars", 0.0) or 0.0
    weeks = {}
    for (year, week), vals in sorted(weekly.items()):
        monday = date.fromisocalendar(year, week, 1)
        weeks[monday.isoformat()] = {
            "kwh": round(vals["kwh"], 3),
            "cost_by_period": {p: round(c, 2) for p, c in vals["cost_by_period"].items()},
            "supply_charge": round(supply_per_day * len(vals["dates"]), 2),
        }
    return weeks, last_date


def build_weekly_chart(result_files, tariff):
    """
    Build the data structure for the last-12-months weekly diverging bar chart.
    Returns None if no detail data is available.
    """
    aggregate = defaultdict(lambda: {"kwh": 0.0, "period_costs": defaultdict(float), "supply": 0.0})
    global_last = None

    for fpath in result_files:
        fname = os.path.basename(fpath)
        stem, _ = os.path.splitext(fname)
        if stem != "detail" and not stem.startswith("detail_"):
            continue
        register = stem[len("detail"):].lstrip("_") or None
        reg_tariff = register_tariff_for(tariff, register)
        weeks, last_date = parse_detail_weekly(fpath, reg_tariff)
        if not weeks:
            continue
        if last_date and (global_last is None or last_date > global_last):
            global_last = last_date
        for week_start, vals in weeks.items():
            aggregate[week_start]["kwh"] += vals["kwh"]
            for p, c in vals["cost_by_period"].items():
                aggregate[week_start]["period_costs"][p] += c
            aggregate[week_start]["supply"] += vals["supply_charge"]

    if not aggregate or global_last is None:
        return None

    cutoff = global_last - timedelta(days=365)
    labels = sorted(
        w for w in aggregate
        if date.fromisoformat(w) >= cutoff - timedelta(days=6)
    )
    if not labels:
        return None

    kwh = [round(aggregate[l]["kwh"], 2) for l in labels]

    period_totals = defaultdict(float)
    for l in labels:
        for p, c in aggregate[l]["period_costs"].items():
            period_totals[p] += c

    categories = sorted(period_totals.keys(), key=lambda p: -period_totals[p])
    has_supply = any(aggregate[l]["supply"] > 0 for l in labels)
    if has_supply:
        categories.append("Supply charge")

    cost_series = {}
    for cat in categories:
        cost_series[cat] = [
            round(
                aggregate[l]["period_costs"].get(cat, 0.0)
                + (aggregate[l]["supply"] if cat == "Supply charge" else 0.0),
                2,
            )
            for l in labels
        ]

    max_cost = max(sum(cost_series[cat][i] for cat in categories) for i in range(len(labels)))

    return {
        "labels": labels,
        "kwh": kwh,
        "cost_series": cost_series,
        "categories": categories,
        "max_kwh": max(kwh) if kwh else 0,
        "max_cost": round(max_cost, 2),
    }


def clean_zip_name(filename):
    base = os.path.splitext(os.path.basename(filename))[0]
    base = re.sub(r"[_\-\s.]*(config|configs|configuration)[_\-\s.]*", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[_\-\s.]+", "_", base).strip("_")
    if not base:
        base = "results"
    return f"{base}.zip"


def build_comparison(result_a, result_b, selected_register=None):
    rows = []
    for ta in result_a["summary_totals"]:
        for tb in result_b["summary_totals"]:
            if ta["summary_file"] == tb["summary_file"]:
                if selected_register:
                    register = selected_register
                else:
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


# ---------------------------------------------------------------------------
# REQUEST LOGGING (DEBUG ONLY)
# ---------------------------------------------------------------------------


if DEBUG:

    @app.before_request
    def log_request():
        logger.debug(
            f"Request {request.method} {request.path} from {request.remote_addr}"
        )

    @app.after_request
    def log_response(response):
        logger.debug(
            f"Response {response.status_code} for {request.method} {request.path}"
        )
        return response


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------


@app.route("/health")
def health():
    return jsonify({"status": "ok", "auth_enabled": AUTH_ENABLED, "debug": DEBUG})


@app.route("/")
@requires_auth
def index():
    return render_template("index.html")


@app.route("/parse-csv", methods=["POST"])
@requires_auth
@limiter.limit("30 per minute")
def parse_csv():
    logger.info(f"parse-csv from {request.remote_addr}")
    file = request.files.get("usage_csv")
    if not file:
        logger.warning("parse-csv missing file")
        return jsonify({"error": "No file uploaded"}), 400

    try:
        validate_file_size(file, MAX_CSV_SIZE)
        validate_extension(file.filename, "usage_csv")
        start, end, days = parse_csv_date_range(file)
        logger.info(f"parse-csv success: {start} to {end}, {days} days")
        return jsonify(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": days,
            }
        )
    except Exception as e:
        logger.warning(f"parse-csv error: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/parse-config", methods=["POST"])
@requires_auth
@limiter.limit("30 per minute")
def parse_config():
    logger.info(f"parse-config from {request.remote_addr}")
    file = request.files.get("tariff_yaml")
    if not file:
        logger.warning("parse-config missing file")
        return jsonify({"error": "No file uploaded"}), 400

    try:
        validate_file_size(file, MAX_YAML_SIZE)
        validate_extension(file.filename, "tariff_yaml")
        content = file.stream.read().decode("utf-8")
        file.stream.seek(0)
        data = yaml.safe_load(content) or {}
        validate_tariff(data)
        plan_name = data.get("plan_name", "(unnamed plan)")
        registers = []
        if isinstance(data.get("registers"), dict):
            registers = sorted(data["registers"].keys())
        logger.info(f"parse-config success: {plan_name}, registers={registers}")
        return jsonify({"plan_name": plan_name, "registers": registers})
    except Exception as e:
        logger.warning(f"parse-config error: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/calculate", methods=["POST"])
@requires_auth
@limiter.limit("10 per minute")
def calculate():
    logger.info(f"calculate from {request.remote_addr}")
    cleanup_old_jobs()

    usage_file = request.files.get("usage_csv")
    tariff_file = request.files.get("tariff_yaml")
    tariff_2_file = request.files.get("tariff_yaml_2")

    # Force the register value to uppercase.
    register = request.form.get("register", "").strip().upper() or None

    if not usage_file or not tariff_file:
        logger.warning("calculate missing required files")
        return jsonify({"error": "Both a usage CSV and a tariff YAML are required."}), 400

    try:
        validate_file_size(usage_file, MAX_CSV_SIZE)
        validate_extension(usage_file.filename, "usage_csv")
        validate_file_size(tariff_file, MAX_YAML_SIZE)
        validate_extension(tariff_file.filename, "tariff_yaml")

        usage_filename = safe_filename(usage_file.filename)
        tariff_filename = safe_filename(tariff_file.filename)

        if tariff_2_file:
            validate_file_size(tariff_2_file, MAX_YAML_SIZE)
            validate_extension(tariff_2_file.filename, "tariff_yaml")
            tariff_2_filename = safe_filename(tariff_2_file.filename)

    except ValueError as e:
        logger.warning(f"calculate validation error: {e}")
        return jsonify({"error": str(e)}), 400

    job_id, job_dir = create_job_dir()
    logger.info(
        f"Job {job_id} started: usage={usage_filename}, tariff={tariff_filename}, register={register}"
    )

    usage_path = os.path.join(job_dir, usage_filename)
    tariff_path = os.path.join(job_dir, tariff_filename)
    usage_file.save(usage_path)
    tariff_file.save(tariff_path)

    configs = [(tariff_path, "config1", tariff_filename)]
    if tariff_2_file:
        tariff_2_path = os.path.join(job_dir, tariff_2_filename)
        tariff_2_file.save(tariff_2_path)
        configs.append((tariff_2_path, "config2", tariff_2_filename))

    configs_named = []
    for path, label, original_name in configs:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        validate_tariff(data)
        data = sanitize_tariff(data)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
        plan_name = data.get("plan_name", "(unnamed plan)")
        configs_named.append((path, label, original_name, plan_name, data))

    results = []
    for path, label, original_name, plan_name, data in configs_named:
        output_dir = os.path.join(job_dir, label, "output")
        os.makedirs(output_dir, mode=0o700, exist_ok=True)

        stdout, stderr, rc = run_calculation_sandboxed(
            usage_csv=usage_path,
            tariff_yaml=path,
            output_dir=output_dir,
            register=register,
        )

        if rc != 0:
            logger.error(f"Job {job_id} {label} failed: {stderr}")
            return jsonify(
                {
                    "error": f"{label} ({plan_name}) calculation failed.",
                    "output": stdout + "\n" + stderr,
                }
            ), 500

        result_files = sorted(glob.glob(os.path.join(output_dir, "*.csv")))
        logger.info(f"Job {job_id} {label} produced {len(result_files)} files")
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
        load_profiles = build_load_profiles(result_files)
        weekly_chart = build_weekly_chart(result_files, data)
        results.append(
            {
                "label": label,
                "config_file": original_name,
                "plan_name": plan_name,
                "zip_name": zip_name,
                "zip_url": f"/download-zip/{job_id}/{label}?download_name={zip_name}",
                "output": stdout,
                "files": file_entries,
                "summary_totals": summary_totals,
                "load_profiles": load_profiles,
                "weekly_chart": weekly_chart,
            }
        )

    comparison = []
    comparison_note = ""
    if len(results) == 2:
        comparison = build_comparison(results[0], results[1], selected_register=register)
        if not comparison:
            comparison_note = "Comparison could not be built: the two configs produced different register outputs."

    logger.info(f"Job {job_id} completed")
    return jsonify(
        {
            "job_id": job_id,
            "configs": results,
            "comparison": comparison,
            "comparison_note": comparison_note,
        }
    )


@app.route("/download/<job_id>/<config_label>/<filename>")
@requires_auth
@limiter.limit("60 per minute")
def download(job_id, config_label, filename):
    try:
        validate_uuid(job_id)
        safe_component(config_label, allow_list={"config1", "config2"})
        filename = safe_filename(filename)
    except ValueError as e:
        logger.warning(f"download validation error: {e}")
        return jsonify({"error": str(e)}), 400

    file_path = os.path.join(JOBS_DIR, job_id, config_label, "output", filename)
    file_path = os.path.realpath(file_path)
    base_path = os.path.realpath(JOBS_DIR)
    if not file_path.startswith(base_path + os.sep):
        logger.warning(f"download path traversal attempt: {file_path}")
        return jsonify({"error": "Invalid path"}), 400

    if not os.path.exists(file_path):
        return "File not found", 404
    return send_file(file_path, as_attachment=True, download_name=filename)


@app.route("/download-zip/<job_id>/<config_label>")
@requires_auth
@limiter.limit("60 per minute")
def download_zip(job_id, config_label):
    try:
        validate_uuid(job_id)
        safe_component(config_label, allow_list={"config1", "config2"})
    except ValueError as e:
        logger.warning(f"download-zip validation error: {e}")
        return jsonify({"error": str(e)}), 400

    config_dir = os.path.join(JOBS_DIR, job_id, config_label, "output")
    config_dir = os.path.realpath(config_dir)
    base_path = os.path.realpath(JOBS_DIR)
    if not config_dir.startswith(base_path + os.sep):
        logger.warning(f"download-zip path traversal attempt: {config_dir}")
        return jsonify({"error": "Invalid path"}), 400

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


@app.errorhandler(Exception)
def handle_error(e):
    logger.exception("Unhandled error")
    return jsonify({"error": "An unexpected error occurred"}), 500


@app.errorhandler(429)
def handle_rate_limit(e):
    return jsonify({"error": "Rate limit exceeded. Please slow down."}), 429


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=DEBUG)
