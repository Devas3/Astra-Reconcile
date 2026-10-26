# Vectura Reconcile V3

Deterministic, event-driven freight and service-charge settlement. No LLMs in any financial math.
Everything free / open source, no Docker.

## Stack
FastAPI + Uvicorn · Pydantic v2 / pydantic-settings · SQLAlchemy 2 + Alembic ·
SQLite (default) or PostgreSQL (free, native install) · ReportLab (PDF) · httpx (EIA API) · pytest + ruff + GitHub Actions.

## No hardcoded business data
| What | Where |
|---|---|
| Rates, free time, rounding, tiers, packages (Rate-Con) | `contracts` table <- `data/seed/contracts.json` |
| Charge catalog, ghost-fee flags, conflicting clock pairs | `charge_catalog`, `conflict_rules` <- `catalog.json` |
| DOE diesel index | `fuel_index` table, synced from the EIA API (`python -m vectura.eia_fuel_index`) |
| UI scenarios, slider range, step | `scenarios` table, `/config` endpoint |
| URLs, keys, tolerances, DB | env vars `VECTURA_*` (see `.env.example`) |

## Run
```
pip install -r requirements.txt
copy .env.example .env        # edit
powershell -ExecutionPolicy Bypass -File scripts/run.ps1    # migrate + seed + serve on http://localhost:8000 (single process: Windows cannot share the port across workers). Linux: sh scripts/run.sh (4 workers)
```
Open http://localhost:8000/ . Docs at `/docs`. Health: `/healthz`, `/readyz`.
Real fuel prices: set `VECTURA_EIA_API_KEY` (free at eia.gov/opendata) and run `python -m vectura.eia_fuel_index` weekly (Task Scheduler / cron).
PostgreSQL: set `VECTURA_DATABASE_URL=postgresql+psycopg://user:pw@localhost:5432/vectura`.

## Test
```
pip install -r requirements-dev.txt
pytest --cov=src/vectura && ruff check src tests
```

## Product workflow (per the Reconcile product document)
- **Pre-bill vs bill:** `/provisional` (at departure) and event-driven proposals (when a clock-closing event such as gate release or OTP sign-off arrives via `/events`) are stored. `/actual` compares the carrier bill with both, per line and in total.
- **Evidence quality:** events carry source, local zone, UTC time, and any later correction (original kept, audited). Source reliability ranks live in `source_reliability`. Conflicting sources are all shown; none is silently chosen; missing facility proof is never guessed.
- **Contract-defined clocks:** each contract states its clock start rule (`APPOINTMENT_IF_ON_TIME`, `CHECK_IN`, `DOCK_START`, rail `PLACEMENT`/`CONSTRUCTIVE_PLACEMENT`) and effective dates; the version in force on the movement date is used. No terms -> contract review, never an assumed rate.
- **Every line** shows claimed / supported / variance / action plus reason code, decision owner, next action and the evidence facts (policy table `action_policies`).
- **Appointment-constrained days** (demurrage, documented pickup attempts) are split into a dispute-review line, never auto-removed.
- **Cases and decisions:** accept, dispute, carrier response, write-off (large ones need a manager), override, mark paid. Roles and permissions come from the DB; API keys map to users and roles (`VECTURA_API_KEYS={"key":{"user":"ann","role":"analyst"}}`). Every manual change is written to `/audit-log` with who, from/to, reason and time.
- **Prevention:** `/prevention` lists open truck movements with free time running out or exceeded, or no arrival after the appointment.
- **Modes:** truckload, ocean (demurrage, detention), storage, rail demurrage, LTL charges with required proofs, service jobs.

## Integrations, evidence, outcomes and administration
- **Inbound adapters** (`POST /integrations/{source}/events`): TMS, WMS/yard/gate and GPS payload shapes are described by mapping rows in `integration_sources` (field paths + event-code map), so a new system needs a database row, not code. Unmapped codes are reported, never guessed. Shared secrets come from env vars named in the row (`VECTURA_SRC_*_SECRET`).
- **Outbound outbox**: settlement-ready, correction-requested, dispute-raised, payment-recorded and prevention alerts are written to `outbox` in the same transaction as the change, then delivered with retries and an `Idempotency-Key` by `python -m vectura.integrations` (or `POST /outbox/dispatch`). Targets (finance/ERP, carrier workflow, TMS) live in `integration_targets`, disabled by default; tokens come from `VECTURA_TGT_*_TOKEN`.
- **Reference matching**: the bill's carrier / BOL / container must match the movement's recorded events, and an invoice number can be processed only once.
- **Holds, calendar, exceptions**: verified terminal/customs holds pause the demurrage clock; unverified hold days are held back (`REQUEST_MISSING_PROOF`). Facility/terminal non-working days (`non_working_days`) are applied automatically from `evidence.location`. Contract exceptions (with responsibility and required proof) shorten the clock only when proven; anything else goes to exception review, never an automatic finding.
- **Evidence pack**: each line cites the contract clause it was priced under; documents (appointment confirmation, gate record...) are uploaded to `/documents` (SHA-256, size, uploader) and listed in the dispute memo.
- **Outcomes**: `AGREE_SETTLEMENT` records the carrier-agreed amount; `MARK_PAID` records the paid amount and reference and must match the agreed/supported value.
- **Manager report**: `/reports/repeat-causes` groups causes by facility and flags repeats.
- **Administration** (role `admin`): `/admin/contracts` (new versions, previous one ends the day before), `/admin/roles`, `/admin/source-reliability`, `/admin/calendar`, `/admin/charges`; every change is audited with a reason.
- **More modes**: LTL redelivery/reconsignment/storage (required proofs), intermodal ramp storage (notice or availability start) and equipment detention, rail demurrage.
