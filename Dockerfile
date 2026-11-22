# ── Job Hunter — cloud image (dashboard + 24/7 discovery loop) ──────────────
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends chromium fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY job_hunter.py rules.json entrypoint.sh ./
COPY jobs.db /app/jobs.seed.db
RUN chmod +x entrypoint.sh && mkdir -p /data
ENV DB_PATH=/data/jobs.db HOST=0.0.0.0 PORT=8080 SCAN_INTERVAL_MINUTES=30 CHROME_CONCURRENCY=2 \
    PYTHONUNBUFFERED=1 TZ=Asia/Kolkata
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=4)"
ENTRYPOINT ["./entrypoint.sh"]
