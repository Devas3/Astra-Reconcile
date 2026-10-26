# Start Reconcile (no Docker): migrate, seed reference data, serve.
# Windows cannot share one listening socket across uvicorn workers (WinError 10022), so the default is a
# single process. Override with $env:VECTURA_WORKERS=4 only on Linux/macOS (see run.sh).
$env:PYTHONPATH = "src"
$port = if ($env:VECTURA_PORT) { $env:VECTURA_PORT } else { "8000" }
$bind = if ($env:VECTURA_HOST) { $env:VECTURA_HOST } else { "127.0.0.1" }
python -m alembic upgrade head
python -m vectura.seed
python -m vectura.eia_fuel_index   # refresh live diesel prices when VECTURA_EIA_API_KEY is set (never blocks startup)
Write-Host "Open http://localhost:$port  (Ctrl+C to stop)"
python -m uvicorn vectura.api:app --host $bind --port $port
