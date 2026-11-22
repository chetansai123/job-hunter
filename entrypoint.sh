#!/bin/sh
# First boot on a fresh volume: start from the jobs/posts you already have locally.
[ -f "$DB_PATH" ] || cp /app/jobs.seed.db "$DB_PATH"
exec python3 -u job_hunter.py --serve --host 0.0.0.0 --port "${PORT:-8080}"
