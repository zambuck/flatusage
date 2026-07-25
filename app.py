#!/usr/bin/env python3
import glob
import io
import os
import sys
import uuid

from flask import Flask, render_template, request, send_file, jsonify

import flat_usage_tou_calculator as calc

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB max upload

JOBS_DIR = os.environ.get("JOBS_DIR", "/tmp/flatusage-jobs")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/calculate", methods=["POST"])
def calculate():
    usage_file = request.files.get("usage_csv")
    tariff_file = request.files.get("tariff_yaml")
    register = request.form.get("register", "").strip() or None

    if not usage_file or not tariff_file:
        return jsonify({"error": "Both a usage CSV and a tariff YAML are required."}), 400

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    usage_path = os.path.join(job_dir, "usage.csv")
    tariff_path = os.path.join(job_dir, "tariff.yaml")
    output_dir = os.path.join(job_dir, "output")

    usage_file.save(usage_path)
    tariff_file.save(tariff_path)

    old_stdout = sys.stdout
    sys.stdout = captured = io.StringIO()
    try:
        calc.run_calculation(usage_path, tariff_path, output_dir, register)
    except Exception as e:
        sys.stdout = old_stdout
        return jsonify({"error": str(e), "output": captured.getvalue()}), 500
    finally:
        sys.stdout = old_stdout

    result_files = sorted(glob.glob(os.path.join(output_dir, "*.csv")))
    filenames = [os.path.basename(f) for f in result_files]

    return jsonify({
        "job_id": job_id,
        "output": captured.getvalue(),
        "files": filenames,
    })


@app.route("/download/<job_id>/<filename>")
def download(job_id, filename):
    file_path = os.path.join(JOBS_DIR, job_id, "output", filename)
    if not os.path.exists(file_path):
        return "File not found", 404
    return send_file(file_path, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
