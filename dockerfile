FROM python:3.12-slim

RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY flat_usage_tou_calculator.py .
COPY app.py .
COPY templates/ ./templates/

RUN mkdir -p /tmp/flatusage-jobs && chown -R appuser:appuser /app /tmp/flatusage-jobs
USER appuser

EXPOSE 5000

ENV JOBS_DIR=/tmp/flatusage-jobs
ENV PYTHONDONTWRITEBYTECODE=1
ENV HOME=/tmp

CMD ["gunicorn", "-b", "0.0.0.0:5000", "--timeout", "120", "--workers", "2", "app:app"]
