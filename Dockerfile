# ── Job Hunter v3 — Docker image ────────────────────────────────────────────
# Runs the dashboard + background discovery loop on any cloud/server.
# No pip packages needed — Python stdlib only.
FROM python:3.11-slim

WORKDIR /app

# Copy project files
COPY job_hunter.py rules.json ./

# Volume for SQLite persistence (mount at /data in cloud)
RUN mkdir -p /data
VOLUME ["/data"]

ENV DB_PATH=/data/jobs.db \
    HOST=0.0.0.0 \
    PORT=8080 \
    SCAN_INTERVAL_MINUTES=30

EXPOSE 8080

# Health-check so cloud platforms know when the service is up
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/stats', timeout=4)"

CMD ["python3", "job_hunter.py", "--serve"]
