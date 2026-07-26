#!/usr/bin/env python3
"""
Hardened Flask web front-end for the flatusage TOU calculator.

Set APP_USER and APP_PASSWORD env vars to enforce HTTP Basic Auth.
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
from datetime import datetime, timedelta
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

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB total request
app.config["PROPAGATE_EXCEPTIONS"] = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("flatusage")

# ---------------------------------------------------------------------------
# SECURITY CONFIG
# ---------------------------------------------------------------------------

JOBS_DIR = os.environ.get("JOBS_DIR", "/tmp/flatusage-jobs")
JOB_MAX_AGE_MINUTES = int(os.environ.get("JOB_MAX_AGE_MINUTES", "60"))
CALC_TIMEOUT_SECONDS = int(os.environ.get("CALC_TIMEOUT_SECONDS", "120"))
MAX_YAML_SIZE = 1024 * 1024  # 1 MB
MAX_CSV_SIZE = 100 * 1024 * 1024  # 100 MB

ALLOWED_EXTENSIONS = {
    "usage_csv": {".csv"},
    "tariff_yaml": {".yaml", ".yml"},
}

SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
SAFE_COMPONENT_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

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

# ---------------------------------------------------------------------------
# AUTHENTICATION
# ---------------------------------------------------------------------------

APP_USER = os.environ.get("APP_USER")
APP_PASSWORD = os.environ.get("APP_PASSWORD")


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not APP_USER or not APP_PASSWORD:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.username != APP_USER or auth.password != APP_PASSWORD:
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="flatusage"'},
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

Talisman(
    app,
    force_https=not app.debug,
    strict_transport_security=not app.debug,
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
    """Prefix string fields that could trigger formula injection in spreadsheets."""
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
    """Ensure only expected keys are present in the tariff YAML."""
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
    """
    Sanitize string fields in the tariff config that may end up in CSV output.
    Returns a new dict; does not mutate the input.
    """
    data = yaml.safe_load(yaml.safe_dump(data))  # deep copy
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
            # quick parser for dd/mm/yyyy hh:mm:ss AM/PM
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
    for entry in os.scandir(JOBS_DIR):
        if entry.is_dir() and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry.path, ignore_errors=True)


def create_job_dir():
    os.makedirs(JOBS_DIR, mode=0o700, exist_ok=True)
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, mode=0o700, exist_ok=False)
    return job_id, job_dir


# ---------------------------------------------------------------------------
# CALCULATOR EXECUTION
# ---------------------------------------------------------------------------


def run_calculation_sandboxed(usage_csv, tariff_yaml, output_dir, register=None):
    cmd = [
        sys.executable,
        "flat_usage_tou_calculator.py",
        usage_csv,
        tariff_yaml,
        "--out-summary", os.path.join(output_dir, "summary.csv"),
        "--out-detail", os.path.join(output_dir, "detail.csv"),
    ]
    if register:
        cmd.extend(["--register", register])

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=CALC_TIMEOUT_SECONDS,
        env=env,
    )
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


def clean_zip_name(filename):
    base = os.path.splitext(os.path.basename(filename))[0]
    base = re.sub(r"[_\-\s.]*(config|configs|configuration)[_\-\s.]*", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[_\-\s.]+", "_", base).strip("_")
    if not base:
        base = "results"
    return f"{base}.zip"


def build_comparison(result_a, result_b):
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


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------


@app.route("/")
@requires_auth
def index():
    return render_template("index.html")


@app.route("/parse-csv", methods=["POST"])
@requires_auth
@limiter.limit("30 per minute")
def parse_csv():
    file = request.files.get("usage_csv")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    try:
        validate_file_size(file, MAX_CSV_SIZE)
        validate_extension(file.filename, "usage_csv")
        start, end, days = parse_csv_date_range(file)
        return jsonify(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "days": days,
            }
        )
    except Exception as e:
        logger.warning(f"CSV parse error from {request.remote_addr}: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/parse-config", methods=["POST"])
@requires_auth
@limiter.limit("30 per minute")
def parse_config():
    file = request.files.get("tariff_yaml")
    if not file:
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
        return jsonify({"plan_name": plan_name, "registers": registers})
    except Exception as e:
        logger.warning(f"Config parse error from {request.remote_addr}: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/calculate", methods=["POST"])
@requires_auth
@limiter.limit("10 per minute")
def calculate():
    cleanup_old_jobs()

    usage_file = request.files.get("usage_csv")
    tariff_file = request.files.get("tariff_yaml")
    tariff_2_file = request.files.get("tariff_yaml_2")
    register = request.form.get("register", "").strip() or None

    if not usage_file or not tariff_file:
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
        return jsonify({"error": str(e)}), 400

    job_id, job_dir = create_job_dir()
    logger.info(
        f"Job {job_id} started from {request.remote_addr}, "
        f"usage={usage_filename}, tariff={tariff_filename}, register={register}"
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
        configs_named.append((path, label, original_name, plan_name))

    results = []
    for path, label, original_name, plan_name in configs_named:
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
                "output": stdout,
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

    logger.info(f"Job {job_id} completed successfully")
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
        return jsonify({"error": str(e)}), 400

    file_path = os.path.join(JOBS_DIR, job_id, config_label, "output", filename)
    file_path = os.path.realpath(file_path)
    base_path = os.path.realpath(JOBS_DIR)
    if not file_path.startswith(base_path + os.sep):
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
        return jsonify({"error": str(e)}), 400

    config_dir = os.path.join(JOBS_DIR, job_id, config_label, "output")
    config_dir = os.path.realpath(config_dir)
    base_path = os.path.realpath(JOBS_DIR)
    if not config_dir.startswith(base_path + os.sep):
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
    app.run(host="0.0.0.0", port=5000, debug=False)
