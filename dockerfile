FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY flat_usage_tou_calculator.py .
COPY app.py .
COPY templates/ ./templates/

EXPOSE 5000

ENV JOBS_DIR=/tmp/flatusage-jobs

CMD ["gunicorn", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
