#!/bin/bash
set -e

echo "Running Alembic migrations..."
alembic upgrade head

echo "Starting Gunicorn with Uvicorn workers..."
exec gunicorn app.main:app \
  -k uvicorn.workers.UvicornWorker \
  -w "${WORKERS:-2}" \
  --bind 0.0.0.0:8000 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
