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
ENV PYTHONUNBUFFERED=1
ENV HOME=/tmp
ENV FORCE_HTTPS=false
ENV DEBUG=false

CMD ["gunicorn", "-b", "0.0.0.0:5000", \
     "--timeout", "180", \
     "--workers", "2", \
     "--threads", "4", \
     "--worker-class", "gthread", \
     "--worker-tmp-dir", "/tmp", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info", \
     "app:app"]
