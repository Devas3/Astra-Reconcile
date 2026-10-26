#!/usr/bin/env sh
set -e
export PYTHONPATH=src
python -m alembic upgrade head
python -m vectura.seed
exec python -m uvicorn vectura.api:app --host 0.0.0.0 --port 8000 --workers 4
