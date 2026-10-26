# Vectura Reconcile: Project Guide

**Purpose of this document:** to let you explain the project to your manager with confidence. Every number, table, endpoint and example in it was generated from the running system, not written from memory. Section 13 contains the questions your manager is most likely to ask, with straight answers (including the weak points).


## Contents

1. The project in one page
2. Business background in plain words
3. What we built, phase by phase
4. Architecture and technology
5. The database: every table and column
6. Where data comes from and where it goes
7. How data is retrieved (the queries behind each decision)
8. The calculation rules (formulas)
9. The full worked example, step by step with real data
10. How the 'simulation' and scenarios work
11. Fraud checks and controls
12. Roles, security and audit
13. Questions your manager will ask (with answers)
14. Honest limitations and what comes next
15. How to run, demo and test


# 1. The project in one page

**Vectura Reconcile checks a carrier's bill against the contract and the recorded facts, and tells you how much you should really pay, why, and who should act on the difference.**

It is built for the **paying side** (the shipper or consumer), not the carrier. A carrier (trucking, shipping, rail or service company) is the **supplier** who sends the bill. We are the **customer** who decides what to pay.

| Question | Answer |
|---|---|
| What goes in? | A carrier bill (the claimed charges) plus evidence: timestamps from gate, warehouse, GPS and appointment systems. |
| What rules does it use? | The signed contract (free time, hourly rates, daily tiers, clauses), stored as data in the database. |
| What comes out? | For every charge: claimed, supported, variance, action, reason, owner, next step, evidence facts, contract clause. |
| Is any AI used? | No. Every amount is plain arithmetic on stored rules, so the same inputs always give the same answer. |
| What does it protect against? | Over-billing, duplicate bills, charges nobody agreed to, padded hours, conflicting timestamps, wrong fuel surcharge. |
| What happens after the check? | A case is opened for each charge. People accept, dispute, agree, write off or mark paid. Every change is audited. |
| What is it not? | Not a replacement for your TMS, WMS, ERP or payment system. It reads facts from them and sends decisions back. |

**Size of the build:** 18 Python modules (2,901 lines), 23 database tables, 30 API endpoints, one web dashboard, and 635 lines of tests (50 automated tests, about 92% code coverage).


# 2. Business background in plain words

| Term | Meaning | Example |
|---|---|---|
| Shipper / consumer | The company sending or receiving goods and paying the carrier. This is our user. | Your company |
| Carrier / supplier | The company moving the goods and sending the bill. | A trucking company |
| BOL (bill of lading) | The shipment's reference number. | BOL-1001 |
| Appointment | The time slot the warehouse gave the truck. | 09:00 |
| Free time | Waiting time the carrier may not charge for. | 2 hours |
| Detention | A fee for a truck waiting longer than free time. | $75 per hour after 2 hours |
| Demurrage | The same idea for a container sitting at a port, charged per day. | $250 per day after the last free day |
| Linehaul | The base freight charge for the distance. | $2.50 per mile |
| Fuel surcharge | A fuel add-on tied to the weekly diesel price. | (price - base) / mpg per mile |
| Accessorial | An extra charge on top: liftgate, lumper, layover. | Liftgate $50 |
| Ghost fee | An extra charge nobody authorized and nobody can prove. | Liftgate with no contract row and no receipt |
| Event | A recorded fact with a time and a source. | 'Check-in 08:55, from the warehouse system' |
| Evidence | The set of events and documents used to price a charge. | Gate-in, check-in, gate-out |
| Case | The work item a person handles for one charge. | DET for movement DEMO-001 |
| Rate-Con | The contract or rate confirmation that authorizes charges. | contracts table |


# 3. What we built, phase by phase

| Phase | What was delivered |
|---|---|
| 1. Proof of concept | Five modules from the original brief: charge clock engine (truck detention, ocean demurrage and detention, storage, service jobs), fraud guardrails, standard output schema, REST endpoints, first dashboard, and the seven required tests. |
| 2. Production-grade, free stack | Moved everything to a database (SQLAlchemy and Alembic migrations), settings from environment variables, API-key security, JSON logging, CI file. Removed hardcoded business data; rates and rules became tables loaded from JSON files. No Docker, no paid services. |
| 3. Product document features | Event store with sources, time zones and corrections. Contract-defined clock start rules. Conflicting evidence shown, never silently resolved. Owners and next actions per line. Cases, roles, permissions, audit trail. Prevention alerts. Pre-bill versus bill comparison. |
| 4. Closing the gaps | Inbound adapters (TMS, WMS, GPS) described by data, outbound outbox, reference and invoice-number matching, terminal holds, contract exceptions, facility holiday calendar, clause citations, document attachments, agreed and paid outcomes, repeat-cause report, admin API, rail, LTL and intermodal. |
| 5. Usability and data | Redesigned dashboard with live what-if preview (dry run) and a draggable evidence timeline. Live diesel prices from the U.S. EIA API. Presentation deck and demo scripts. |


# 4. Architecture and technology


### 4.1 The big picture

```

  SOURCES OF FACTS                          VECTURA RECONCILE                           PEOPLE AND SYSTEMS

  TMS / WMS / GPS  --(inbound adapters)-->  movement_events  \
  (appointment, gate, check-in)                                 \
                                                                 >-- Reconciler (rule_engine.py) --> settlements, cases
  Carrier bill (JSON from the UI/API) ----------------------- /        |   uses:                          |
                                                                       |   contracts (rates, clauses)     +--> dashboard (browser)
  EIA diesel price (API) --> fuel_index                                |   fuel_index, catalog, calendar  +--> dispute memo (PDF)
                                                                       |   fraud_guardrails (checks)      +--> outbox --> finance / carrier / TMS
  Seed JSON files --> reference tables (rules)                         +-- decisions (roles, audit_log)
```


### 4.2 Technology stack (all free and open source)

| Layer | Technology | Why |
|---|---|---|
| Language | Python 3.13 (3.11 or newer) | Readable, strong for rule logic and finance maths. |
| Web API | FastAPI with Uvicorn | Automatic request validation and interactive docs at /docs. |
| Validation | Pydantic v2 | Every request and response has a defined shape. Bad data is rejected. |
| Settings | pydantic-settings, .env file | Port, database, keys and tolerances come from the environment. |
| Database access | SQLAlchemy 2 | Same code works on SQLite and PostgreSQL. |
| Migrations | Alembic (3 migration files) | Versioned, repeatable database changes. |
| Database | SQLite (WAL mode) today, PostgreSQL-ready | Zero install for development; PostgreSQL by changing one setting. |
| Money maths | Python Decimal | Exact cents, half-up rounding, no floating-point surprises. |
| External calls | httpx | EIA diesel prices in; outbound outbox deliveries out. |
| PDF | ReportLab | Dispute memo. Chosen over PyMuPDF because of its AGPL license. |
| Dashboard | Plain HTML, CSS and JavaScript (no build step) | Served by the same app at the root address. |
| Quality | pytest (50 tests), ruff, GitHub Actions file | Regression safety and consistent code style. |


### 4.3 The code, file by file

| File | Lines | What it does |
|---|---|---|
| src/vectura/__init__.py | 1 | Package marker. |
| src/vectura/admin.py | 126 | Contract versioning, roles, source ranks, calendar, charge catalog, document storage (all audited). |
| src/vectura/api.py | 413 | All web endpoints, request logging, error handling. The doorway for the screen and other systems. |
| src/vectura/charge_clock_engine.py | 463 | The calculators: truck detention, ocean demurrage and detention, storage, rail, intermodal, service. Also the output schema. |
| src/vectura/config.py | 62 | All settings, read from the environment. |
| src/vectura/db.py | 59 | Database engine and sessions. |
| src/vectura/decisions.py | 74 | Accept, dispute, agree, write off, override, mark paid, with permission checks and audit entries. |
| src/vectura/eia_fuel_index.py | 85 | Weekly diesel price lookup and the sync from the EIA API. |
| src/vectura/events.py | 162 | Stores events, picks the most reliable source, records corrections, produces prevention alerts. |
| src/vectura/fraud_guardrails.py | 77 | Duplicate, ghost-fee, overlapping-time and fuel-price checks. |
| src/vectura/integrations.py | 135 | Translates other systems' payloads into events; outbound outbox and its delivery with retries. |
| src/vectura/models.py | 269 | Definition of every database table. |
| src/vectura/pdf_memo.py | 54 | Builds the dispute memo PDF. |
| src/vectura/reports.py | 33 | Repeated-cause-by-facility report. |
| src/vectura/rule_engine.py | 549 | The coordinator (class Reconciler): runs every check and calculation in the right order. The heart of the project. |
| src/vectura/schemas.py | 203 | Shape of every incoming request; converts all times to UTC. |
| src/vectura/security.py | 40 | API-key authentication and role permissions. |
| src/vectura/seed.py | 96 | Loads the JSON rule files into the database tables. |

Other folders: `data/seed/` (the rules as JSON), `migrations/` (database versions), `frontend/index.html` (the dashboard), `tests/` (50 tests), `scripts/` (run scripts), `.env` (secrets, never committed).


# 5. The database: every table and column

The system has **23 tables** in three groups. The database is a single file (`vectura.db`) in the project folder for local use; the same tables work on PostgreSQL. The definitions below were read directly from the code (`models.py`), so they cannot be out of date.


### A. Rules and reference data (loaded from data/seed; changed through the admin API)

**charge_catalog**: Every charge type we know, and whether it needs prior authorization (the ghost-fee rule).

| Column | Type | Key | Can be empty |
|---|---|---|---|
| code | VARCHAR(32) | PK | no |
| description | VARCHAR(200) |  | no |
| requires_authorization | BOOLEAN |  | no |

**conflict_rules**: Pairs of charges that cannot cover the same time (for example detention and layover).

| Column | Type | Key | Can be empty |
|---|---|---|---|
| id | INTEGER | PK | no |
| code_a | VARCHAR(32) | FK to charge_catalog.code | no |
| code_b | VARCHAR(32) | FK to charge_catalog.code | no |

Unique together: (code_a, code_b)

**contracts**: The carrier's terms per charge, with effective dates and the clause text. One row = authorization to bill that charge.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| id | INTEGER | PK | no |
| carrier_id | VARCHAR(64) |  | no |
| charge_code | VARCHAR(32) | FK to charge_catalog.code | no |
| terms | JSON |  | no |
| effective_from | DATE |  | no |
| effective_to | DATE |  | yes |
| clause_ref | VARCHAR(64) |  | yes |
| clause_text | TEXT |  | yes |

Unique together: (carrier_id, charge_code, effective_from)

**fuel_index**: Weekly diesel prices (from the EIA API, or sample rows).

| Column | Type | Key | Can be empty |
|---|---|---|---|
| week_start | DATE | PK | no |
| price | FLOAT |  | no |
| source | VARCHAR(32) |  | no |

**event_types**: Kinds of events, which evidence field each fills, and whether it closes the clock.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| code | VARCHAR(32) | PK | no |
| description | VARCHAR(200) |  | no |
| evidence_group | VARCHAR(24) |  | no |
| evidence_field | VARCHAR(48) |  | no |
| closes_clock | BOOLEAN |  | no |

**source_reliability**: Which sources are accepted for each event type, and their trust rank.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| event_type | VARCHAR(32) | PK, FK to event_types.code | no |
| source | VARCHAR(32) | PK | no |
| rank | INTEGER |  | no |

**action_policies**: For each reason code: owner, next action, starting case status, severity.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| reason_code | VARCHAR(40) | PK | no |
| decision_owner | VARCHAR(120) |  | no |
| next_action | TEXT |  | no |
| initial_status | VARCHAR(32) |  | no |
| severity | INTEGER |  | no |

**role_permissions**: Which permissions each role holds.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| role | VARCHAR(32) | PK | no |
| permission | VARCHAR(48) | PK | no |

**decision_types**: The decision buttons, permissions needed, resulting status, allowed starting statuses.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| code | VARCHAR(32) | PK | no |
| permission | VARCHAR(48) |  | no |
| resulting_status | VARCHAR(32) |  | no |
| requires_reason | BOOLEAN |  | no |
| needs_amount | BOOLEAN |  | no |
| needs_reference | BOOLEAN |  | no |
| allowed_from | JSON |  | no |

**status_outcomes**: Which case statuses send a message to another system.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| status | VARCHAR(32) | PK | no |
| kind | VARCHAR(40) |  | no |

**non_working_days**: Facility, terminal and ramp holidays.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| location | VARCHAR(64) | PK | no |
| day | DATE | PK | no |
| name | VARCHAR(120) |  | no |

**integration_sources**: Inbound adapters: how each system's payload maps to our events.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| name | VARCHAR(48) | PK | no |
| description | VARCHAR(200) |  | no |
| mapping | JSON |  | no |
| auth_env | VARCHAR(64) |  | yes |
| enabled | BOOLEAN |  | no |

**integration_targets**: Outbound destinations and which outcomes they receive.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| name | VARCHAR(48) | PK | no |
| description | VARCHAR(200) |  | no |
| url | VARCHAR(400) |  | no |
| subscribes | JSON |  | no |
| auth_env | VARCHAR(64) |  | yes |
| enabled | BOOLEAN |  | no |

**scenarios**: The demo scenarios shown in the dashboard.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| name | VARCHAR(40) | PK | no |
| label | VARCHAR(120) |  | no |
| description | TEXT |  | no |
| mode | VARCHAR(24) |  | no |
| repeat | INTEGER |  | no |
| sort_order | INTEGER |  | no |
| request | JSON |  | no |


### B. Operational data (written while the system works)

**movement_events**: Every recorded fact: type, source, UTC time, original value, time zone, location, corrections.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| id | INTEGER | PK | no |
| movement_id | VARCHAR(64) |  | no |
| carrier_id | VARCHAR(64) |  | no |
| bol_number | VARCHAR(64) |  | no |
| container_or_job_id | VARCHAR(64) |  | no |
| event_type | VARCHAR(32) | FK to event_types.code | no |
| occurred_at | DATETIME |  | no |
| original_value | VARCHAR(64) |  | no |
| local_tz | VARCHAR(64) |  | no |
| source | VARCHAR(32) |  | no |
| recorded_by | VARCHAR(64) |  | no |
| recorded_at | DATETIME |  | no |
| superseded_by | INTEGER |  | yes |
| location | VARCHAR(64) |  | yes |

**settlements**: Every result produced (PROVISIONAL, PROPOSED, ACTUAL) as stored JSON with a proof ID.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| id | INTEGER | PK | no |
| movement_id | VARCHAR(64) |  | no |
| stage | VARCHAR(16) |  | no |
| proof_id | VARCHAR(32) |  | no |
| payload | JSON |  | no |
| created_at | DATETIME |  | no |

Unique together: (proof_id)

**cases**: One work item per movement and charge, with status, amounts, owner and outcome (agreed, paid).

| Column | Type | Key | Can be empty |
|---|---|---|---|
| id | INTEGER | PK | no |
| movement_id | VARCHAR(64) |  | no |
| charge_code | VARCHAR(32) |  | no |
| claim_hash | VARCHAR(64) |  | no |
| claimed | FLOAT |  | no |
| supported | FLOAT |  | no |
| status | VARCHAR(32) |  | no |
| reason_code | VARCHAR(40) |  | no |
| decision_owner | VARCHAR(120) |  | no |
| next_action | TEXT |  | no |
| location | VARCHAR(64) |  | yes |
| agreed_amount | FLOAT |  | yes |
| paid_amount | FLOAT |  | yes |
| paid_reference | VARCHAR(64) |  | yes |
| updated_at | DATETIME |  | no |

Unique together: (movement_id, charge_code)

**audit_records**: Charges already approved or paid, keyed by a fingerprint. This is how duplicates are detected.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| hash | VARCHAR(64) | PK | no |
| carrier_id | VARCHAR(64) |  | no |
| bol_number | VARCHAR(64) |  | no |
| charge_code | VARCHAR(32) |  | no |
| reference | VARCHAR(64) |  | no |
| status | VARCHAR(16) |  | no |
| movement_id | VARCHAR(64) |  | no |
| created_at | DATETIME |  | no |

**invoice_records**: Carrier invoice numbers already processed.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| id | INTEGER | PK | no |
| carrier_id | VARCHAR(64) |  | no |
| invoice_number | VARCHAR(64) |  | no |
| movement_id | VARCHAR(64) |  | no |
| created_at | DATETIME |  | no |

Unique together: (carrier_id, invoice_number)

**fraud_alerts**: Every alert raised (duplicate, ghost fee, overlap, fuel, reference mismatch).

| Column | Type | Key | Can be empty |
|---|---|---|---|
| id | INTEGER | PK | no |
| movement_id | VARCHAR(64) |  | no |
| alert_type | VARCHAR(40) |  | no |
| charge_code | VARCHAR(64) |  | no |
| detail | TEXT |  | no |
| created_at | DATETIME |  | no |

**documents**: Uploaded evidence files: name, kind, SHA-256 fingerprint, size, uploader.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| id | INTEGER | PK | no |
| movement_id | VARCHAR(64) |  | no |
| kind | VARCHAR(48) |  | no |
| filename | VARCHAR(200) |  | no |
| storage_path | VARCHAR(400) |  | no |
| sha256 | VARCHAR(64) |  | no |
| size_bytes | INTEGER |  | no |
| uploaded_by | VARCHAR(64) |  | no |
| created_at | DATETIME |  | no |


### C. Control data (accountability and delivery)

**audit_log**: Every manual change: who, role, old value, new value, reason, time.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| id | INTEGER | PK | no |
| entity | VARCHAR(32) |  | no |
| entity_id | VARCHAR(64) |  | no |
| field | VARCHAR(48) |  | no |
| old_value | TEXT |  | yes |
| new_value | TEXT |  | yes |
| reason | TEXT |  | no |
| actor | VARCHAR(64) |  | no |
| role | VARCHAR(32) |  | no |
| created_at | DATETIME |  | no |

**outbox**: Messages waiting to be sent to other systems, with status, attempts and last error.

| Column | Type | Key | Can be empty |
|---|---|---|---|
| id | INTEGER | PK | no |
| target | VARCHAR(48) |  | no |
| kind | VARCHAR(40) |  | no |
| movement_id | VARCHAR(64) |  | no |
| payload | JSON |  | no |
| status | VARCHAR(12) |  | no |
| attempts | INTEGER |  | no |
| last_error | TEXT |  | yes |
| created_at | DATETIME |  | no |
| sent_at | DATETIME |  | yes |


### 5.1 How the tables relate

```

  charge_catalog (code) <--- contracts (carrier_id, charge_code, effective_from)      "which charges exist, and the price terms"
  charge_catalog (code) <--- conflict_rules (code_a, code_b)
  event_types (code)    <--- source_reliability (event_type, source, rank)
  event_types (code)    <--- movement_events (movement_id, event_type, source, occurred_at)

  settlements (movement_id, stage)   -- result JSON of each run
  cases (movement_id, charge_code)   -- one per charge, holds status and amounts
  audit_records (hash)               -- fingerprint of carrier + BOL + charge + container  => duplicate detection
  invoice_records (carrier_id, invoice_number)                                            => repeated invoice detection
  audit_log (entity_id, ...)         -- every manual change
  outbox (target, kind, ...)         -- messages to other systems
```

**Why settlements are stored as JSON:** the exact result shown to the user (all lines, facts, reasons) is saved as it was, so you can always show what the system said at that moment, even after rules change. The key fields (movement, stage, proof ID) are real columns for searching.

**Row counts right after seeding (reference data only):**

| Table | Rows |
|---|---|
| action_policies | 18 |
| charge_catalog | 19 |
| conflict_rules | 2 |
| contracts | 13 |
| decision_types | 7 |
| event_types | 8 |
| fuel_index | 4 |
| integration_sources | 3 |
| integration_targets | 3 |
| non_working_days | 4 |
| role_permissions | 33 |
| scenarios | 7 |
| source_reliability | 14 |
| status_outcomes | 5 |


# 6. Where data comes from and where it goes


### 6.1 Coming in

| Source | Mechanism | Lands in | Status today |
|---|---|---|---|
| Rule files (data/seed/*.json) | `python -m vectura.seed` at startup | Reference tables (section 5, group A) | Working. Sample data written from your sir's examples. |
| Carrier bill | POST /api/v1/reconcile/actual | Processed, then settlements, cases, audit_records, fraud_alerts | Working. Input is structured JSON (no document reading). |
| Departure details | POST /provisional | settlements (stage PROVISIONAL) | Working. |
| Operational events | POST /events, or POST /integrations/{tms/wms/telematics}/events | movement_events | Working with sample payloads. No real system is connected yet. |
| Diesel prices | Pull from the EIA API (`python -m vectura.eia_fuel_index`, also run at startup) | fuel_index | Working with a real key; 104 weekly prices loaded. |
| Evidence documents | POST /documents (multipart upload) | File saved under var/documents, row in documents | Working. |
| Decisions and corrections | POST /decisions, /events/{id}/correct, admin endpoints | cases, audit_log, contracts and others | Working. |


### 6.2 Going out

| Output | Mechanism | Status today |
|---|---|---|
| Results to the dashboard | JSON responses | Working. |
| Dispute memo | POST /dispute-memo returns a PDF | Working. |
| Settlement-ready, correction, dispute and payment messages | outbox table, delivered by `integrations.dispatch()` with retries and an idempotency key | Built and tested against a mock server. Targets are disabled and point at placeholder addresses. |
| Prevention alerts to the TMS | GET /prevention?notify=true queues them | Built; target disabled. |
| Outbound call to EIA | HTTP GET for diesel prices | Working. |
| Logs | JSON lines on the console | Working. |


### 6.3 Where things physically sit

| Item | Location |
|---|---|
| Database | `vectura.db` in the project folder (plus `-wal` and `-shm` helper files while running). Switch with VECTURA_DATABASE_URL. |
| Uploaded documents | `var/documents/<movement>/` on disk; metadata in the documents table. |
| Secrets (EIA key, API keys) | `.env` file, excluded from git. |
| Rules | `data/seed/*.json`, copied into the database at startup. |


# 7. How data is retrieved (the queries behind each decision)

Nothing is guessed. Each decision reads specific rows. These are the actual lookups the code performs:

| Decision | What is read | How it is chosen |
|---|---|---|
| Which contract terms apply? | contracts | Rows for this carrier and charge where effective_from is on or before the movement date and effective_to is empty or on or after it; the newest start date wins. The movement's own date is used, never today's date. |
| Is this charge authorized? | charge_catalog + contracts | The catalog says whether the charge requires authorization. A contract row (or an attached receipt) satisfies it. |
| Which timestamp is used when sources disagree? | movement_events + source_reliability | Active events (not superseded) are grouped by event type. The source with the lowest rank number wins. All of them are still listed as facts. Differences above the tolerance setting are flagged. |
| Is this a duplicate? | audit_records | A SHA-256 fingerprint of carrier + BOL + charge code + container/job is the primary key. If a row exists with status APPROVED or PAID, it is a duplicate. |
| Is this invoice number repeated? | invoice_records | Unique on (carrier, invoice number). |
| Do the bill's references match reality? | movement_events | Carrier, BOL and container on the bill are compared with those on the recorded events for the movement. |
| Do two charges overlap? | conflict_rules | Pairs of charge codes that may not cover the same time window; compared with the time claims supplied. |
| What was expected before the bill? | settlements (PROVISIONAL, PROPOSED) | Latest provisional and event-proposal lines per charge code. |
| What diesel price applies? | fuel_index | Find the Monday of the shipment's week; take the newest row on or before it. |
| Which days are holidays? | non_working_days | All days for the location code on the evidence; merged into the day-based charge clocks. |
| Who owns this problem and what is next? | action_policies | Looked up by the line's reason code. |
| May this user press this button? | role_permissions + decision_types | The role must hold the permission the decision requires, and the case status must be one the decision allows. |


# 8. The calculation rules (formulas)

| Charge | Formula | Parameters come from (contracts table) |
|---|---|---|
| Truck detention (DET) | elapsed = release - start; billable = max(0, elapsed - free - excepted); round per rule; amount = billable/60 x hourly rate | start_rule, free_time_minutes, hourly_rate, rounding_rule, exceptions |
| Detention clock start | APPOINTMENT_IF_ON_TIME: appointment if arrival <= appointment, else dock start or check-in. CHECK_IN or DOCK_START: that facility record. | start_rule |
| Rounding | EXACT; CEIL_15_MIN and CEIL_30_MIN round up to the step; NEAREST_HOUR rounds half up. | rounding_rule |
| Ocean demurrage (DEM) | For each day after the last free day: skip terminal-closed days and verified-hold days; hold back unverified-hold days; flag no-appointment days for dispute; day price from the tier of the day number counted from the available date. | daily_rate_tiers |
| Ocean/intermodal detention | Days after gate-out beyond the free days, minus congestion-hold days, times the daily rate. | equipment_free_days, daily_rate |
| Storage | Days after the free days, excluding closed days, times the daily rate. | free_days, daily_rate |
| Rail demurrage | From the contract's start event (placement or constructive placement) plus free days, excluding tariff non-working days; tiered by chargeable day number. | start_event, free_days, daily_rate_tiers |
| Intermodal ramp storage | From ramp availability or notice (contract decides) plus free days; tiered. | start_event, free_days, daily_rate_tiers |
| Service job (SVC) | overtime = max(0, actual hours - package hours); total = base fee + overtime x rate + authorized materials. | base_package_hours, base_package_fee, overtime_hourly_rate |
| Linehaul (LH) | miles x rate per mile (provisional); the bill is compared with this. | rate_per_mile |
| Fuel surcharge (FSC) | per-mile rate = max(0, (diesel price - base price) / mpg), rounded to 3 decimals; amount = rate x miles. | base_price, mpg; price from fuel_index |
| Flat charges (LTL) | A fixed amount, payable only if the required proof documents are attached. | flat_amount, required_proofs |

**Money handling:** all amounts use exact decimal arithmetic and round half-up to the cent. Supported is never more than claimed. Variance = claimed - supported.

**Overall action for a bill** (highest priority wins): any fraud alert or review line gives `HITL_REVIEW` (human in the loop), then any missing proof gives `REQUEST_MISSING_PROOF`, then any partial amount gives `PARTIAL_APPROVE`, otherwise `APPROVE`.


# 9. The full worked example, step by step with real data

This story was executed against a freshly built database and every request, response and table change below was recorded. Carrier **CARR-7**, shipment **DEMO-001**, BOL **BOL-1001**, trailer **TRL-55**, destination warehouse **DC-CHI** (Chicago time zone). Sample contract: detention has 120 free minutes then $75/hour (clause 4.2), linehaul $2.50/mile, fuel base price $1.25 at 6 mpg. For this demo the outbound targets were switched on so that the outbox is visible.

**Starting point:** after loading the rule files the database holds 140 reference rows and no operational data.


### 1. Truck leaves the factory: provisional invoice

The truck leaves. The system prices what we expect to pay from the contract alone: 100 miles x $2.50, plus fuel. Fuel: the diesel price for the week of 10 March is 3.70 in the sample table, so (3.70 - 1.25) / 6 = $0.408 per mile x 100 miles = $40.80.

**Request:** `POST /api/v1/reconcile/provisional`

```
{
  "movement_id": "DEMO-001",
  "bol_number": "BOL-1001",
  "carrier_id": "CARR-7",
  "origin": "Factory",
  "destination": "DC Chicago",
  "origin_departure": "2025-03-10T06:00:00",
  "miles": 100
}
```

**Response (HTTP 200):**

```
{
  "movement_id": "DEMO-001",
  "stage": "PROVISIONAL",
  "total_claimed": 290.8,
  "total_supported": 290.8,
  "total_variance": 0.0,
  "overall_action": "APPROVE",
  "fraud_alerts": [],
  "line_items": [
    {
      "charge_code": "LH",
      "description": "Linehaul freight",
      "amount_claimed": 250.0,
      "amount_supported": 250.0,
      "variance": 0.0,
      "recommended_action": "APPROVE",
      "findings": "Pro-forma linehaul Factory -> DC Chicago, departed 2025-03-10T06:00:00.",
      "proof_references": [],
      "reason_code": "NONE",
      "decision_owner": "Freight audit / finance (under policy)",
      "next_action": "Approve or forward for normal posting",
      "evidence_facts": [],
      "provisional_amount": null,
      "clause": null
    },
    {
      "charge_code": "FSC",
      "description": "Fuel surcharge",
      "amount_claimed": 40.8,
      "amount_supported": 40.8,
      "variance": 0.0,
      "recommended_action": "APPROVE",
      "findings": "DOE index week of 2025-03-10: $0.408/mi x 100.0 mi.",
      "proof_references": [],
      "reason_code": "NONE",
      "decision_owner": "Freight audit / finance (under policy)",
      "next_action": "Approve or forward for normal posting",
      "evidence_facts": [],
      "provisional_amount": null,
      "clause": null
    }
  ],
  "audit_proof_pack_id": "APP-BC40802DC8CCAC2D",
  ... (trimmed)
```

**Database rows added (table: before -> after):** `settlements` 0 -> 1


### 2. TMS reports the confirmed appointment

The transport system tells us the confirmed appointment. The payload is in the TMS's own format; the stored mapping for source `tms` turns `APPT_CONFIRMED` into our `APPOINTMENT` event. The local time 09:00 in Chicago is stored as 14:00 UTC (daylight time began on 9 March).

**Request:** `POST /api/v1/reconcile/integrations/tms/events`

```
{
  "shipmentId": "DEMO-001",
  "carrierCode": "CARR-7",
  "bol": "BOL-1001",
  "equipmentId": "TRL-55",
  "stopCode": "DC-CHI",
  "statuses": [
    {
      "code": "APPT_CONFIRMED",
      "at": "2025-03-10T09:00:00",
      "tz": "America/Chicago"
    }
  ]
}
```

**Response (HTTP 200):**

```
{
  "accepted": 1,
  "skipped_unmapped": [],
  "proposal": null
}
```

**Database rows added (table: before -> after):** `movement_events` 0 -> 1


### 3. Warehouse system reports gate-in and check-in

The warehouse system reports two events (`GATE_IN` becomes `ARRIVAL`, `CHECKED_IN` becomes `CHECK_IN`).

**Request:** `POST /api/v1/reconcile/integrations/wms/events`

```
{
  "movement": "DEMO-001",
  "carrier": "CARR-7",
  "bol": "BOL-1001",
  "trailer": "TRL-55",
  "facility": "DC-CHI",
  "events": [
    {
      "event": "GATE_IN",
      "ts": "2025-03-10T08:45:00",
      "tz": "America/Chicago"
    },
    {
      "event": "CHECKED_IN",
      "ts": "2025-03-10T08:55:00",
      "tz": "America/Chicago"
    }
  ]
}
```

**Response (HTTP 200):**

```
{
  "accepted": 2,
  "skipped_unmapped": [],
  "proposal": null
}
```

**Database rows added (table: before -> after):** `movement_events` 1 -> 3

**What the events table looks like now** (original value kept, UTC used for calculation):

| id | event_type | source | occurred_at (UTC) | as received | time zone | location |
|---|---|---|---|---|---|---|
| 1 | APPOINTMENT | TMS | 2025-03-10 14:00:00 | 2025-03-10T09:00:00 [America/Chicago] | America/Chicago | DC-CHI |
| 2 | ARRIVAL | GATE | 2025-03-10 13:45:00 | 2025-03-10T08:45:00 [America/Chicago] | America/Chicago | DC-CHI |
| 3 | CHECK_IN | WMS | 2025-03-10 13:55:00 | 2025-03-10T08:55:00 [America/Chicago] | America/Chicago | DC-CHI |
| 4 | RELEASE | GATE | 2025-03-10 17:30:00 | 2025-03-10T12:30:00 [America/Chicago] | America/Chicago | DC-CHI |

The source for each event is chosen from `source_reliability`: for arrival, the gate (rank 1) outranks GPS (rank 2); check-in is trusted from the warehouse system (rank 1).


### 4. Prevention check while the truck is still waiting (10:45 local)

While the truck is still waiting, the prevention check asks: how much free time is left? Clock start 14:00 UTC (the appointment, because arrival was earlier), free time 120 minutes, current time 15:45 UTC, so 15 minutes remain.

**Request:** `GET /api/v1/reconcile/prevention?as_of=2025-03-10T15:45:00Z`

**Response (HTTP 200):**

```
[
  {
    "movement_id": "DEMO-001",
    "carrier_id": "CARR-7",
    "alert_type": "PREVENT_APPROACHING",
    "detail": "15 min of free time left (clock start 14:00Z).",
    "minutes_remaining": 15.0,
    "decision_owner": "Transport operations",
    "next_action": "Free time is running out: escalate to the facility or arrange release"
  }
]
```

**Database rows added:** none.


### 5. Warehouse reports gate-out: the clock closes and the system prices it

The warehouse reports gate-out. `RELEASE` is flagged `closes_clock`, so the system immediately prices what the events support and stores it as a PROPOSED settlement. This is the pre-bill for detention: 09:00 to 12:30 is 210 minutes, minus 120 free = 90 billable minutes = 1.5 hours x $75 = **$112.50**.

**Request:** `POST /api/v1/reconcile/integrations/wms/events`

```
{
  "movement": "DEMO-001",
  "carrier": "CARR-7",
  "bol": "BOL-1001",
  "trailer": "TRL-55",
  "facility": "DC-CHI",
  "events": [
    {
      "event": "GATE_OUT",
      "ts": "2025-03-10T12:30:00",
      "tz": "America/Chicago"
    }
  ]
}
```

**Response (HTTP 200):**

```
{
  "accepted": 1,
  "skipped_unmapped": [],
  "proposal": {
    "movement_id": "DEMO-001",
    "stage": "PROPOSED",
    "total_claimed": 112.5,
    "total_supported": 112.5,
    "total_variance": 0.0,
    "overall_action": "APPROVE",
    "fraud_alerts": [],
    "line_items": [
      {
        "charge_code": "DET",
        "description": "Truckload detention",
        "amount_claimed": 112.5,
        "amount_supported": 112.5,
        "variance": 0.0,
        "recommended_action": "APPROVE",
        "findings": "System-generated from verified events: Arrived on/before appointment: clock starts at appointment time. Elapsed 210 min - free 120 - excepted 0 = 90 billable -> 90 min (EXACT) @ $75.00/hr.",
        "proof_references": [
          "arrival:2025-03-10T13:45:00+00:00",
          "check_in:2025-03-10T13:55:00+00:00",
          "clock_start:2025-03-10T14:00:00+00:00",
          "release:2025-03-10T17:30:00+00:00"
        ],
        "reason_code": "NONE",
        "decision_owner": "Freight audit / finance (under policy)",
        "next_action": "Approve or forward for normal posting",
        "evidence_facts": [
          "Arrival at facility (GPS/gate) \u00b7 GATE \u00b7 2025-03-10 08:45 CDT (13:45Z)",
          "Facility check-in completed \u00b7 WMS \u00b7 2025-03-10 08:55 CDT (13:55Z)",
          "Confirmed appointment \u00b7 TMS \u00b7 2025-03-10 09:00 CDT (14:00Z)",
          "Driver released / gate-out \u00b7 GATE \u00b7 2025-03-10 12:30 CDT (17:30Z)"
        ],
  ... (trimmed)
```

**Database rows added (table: before -> after):** `settlements` 1 -> 2, `movement_events` 3 -> 4

**The bill arrives.** The carrier claims four charges on invoice INV-1001: linehaul $250.00, fuel $40.80, detention $262.50 (3.5 hours) and a liftgate fee of $50.00. No timestamps are sent with the bill: the system uses the events it already holds for this movement.


### 6. What-if preview of the carrier's bill (dry run: nothing saved)

A what-if run (dry run). The full calculation executes, then every database write is rolled back. This is what the dashboard does while you drag a time marker. No rows are added.

**Request:** `POST /api/v1/reconcile/actual`

```
{
  "movement_id": "DEMO-001",
  "bol_number": "BOL-1001",
  "carrier_id": "CARR-7",
  "container_or_job_id": "TRL-55",
  "invoice_number": "INV-1001",
  "claimed_charges": [
    {
      "charge_code": "LH",
      "amount": 250.0
    },
    {
      "charge_code": "FSC",
      "amount": 40.8
    },
    {
      "charge_code": "DET",
      "amount": 262.5
    },
    {
      "charge_code": "LFT",
      "amount": 50.0
    }
  ],
  "evidence": {
    "fuel": {
      "ship_date": "2025-03-10",
      "miles": 100
    }
  },
  "dry_run": true
}
```

**Database rows added:** none.

| Charge | Pre-bill | Claimed | Supported | Variance | Action | Reason |
|---|---|---|---|---|---|---|
| LH | $250.00 | $250.00 | $250.00 | $0.00 | APPROVE | NONE |
| FSC | $40.80 | $40.80 | $40.80 | $0.00 | APPROVE | NONE |
| DET | $112.50 | $262.50 | $112.50 | $150.00 | PARTIAL_APPROVE | OVER_CLAIM |
| LFT | - | $50.00 | $0.00 | $50.00 | HITL_REVIEW | GHOST_FEE |
| TOTAL | $403.30 | $603.30 | $403.30 | $200.00 | HITL_REVIEW |  |


### 7. The carrier's bill is submitted for real

The same bill, submitted for real.

**Request:** `POST /api/v1/reconcile/actual`

```
{
  "movement_id": "DEMO-001",
  "bol_number": "BOL-1001",
  "carrier_id": "CARR-7",
  "container_or_job_id": "TRL-55",
  "invoice_number": "INV-1001",
  "claimed_charges": [
    {
      "charge_code": "LH",
      "amount": 250.0
    },
    {
      "charge_code": "FSC",
      "amount": 40.8
    },
    {
      "charge_code": "DET",
      "amount": 262.5
    },
    {
      "charge_code": "LFT",
      "amount": 50.0
    }
  ],
  "evidence": {
    "fuel": {
      "ship_date": "2025-03-10",
      "miles": 100
    }
  }
}
```

**Database rows added (table: before -> after):** `audit_records` 0 -> 3, `cases` 0 -> 4, `fraud_alerts` 0 -> 1, `invoice_records` 0 -> 1, `outbox` 0 -> 3, `settlements` 2 -> 3

| Charge | Pre-bill | Claimed | Supported | Variance | Action | Reason |
|---|---|---|---|---|---|---|
| LH | $250.00 | $250.00 | $250.00 | $0.00 | APPROVE | NONE |
| FSC | $40.80 | $40.80 | $40.80 | $0.00 | APPROVE | NONE |
| DET | $112.50 | $262.50 | $112.50 | $150.00 | PARTIAL_APPROVE | OVER_CLAIM |
| LFT | - | $50.00 | $0.00 | $50.00 | HITL_REVIEW | GHOST_FEE |
| TOTAL | $403.30 | $603.30 | $403.30 | $200.00 | HITL_REVIEW |  |

**How each line was decided:**

- **Linehaul $250.00:** matches the provisional invoice exactly, so approved.
- **Fuel $40.80:** matches the DOE-pegged amount for the week, so approved.
- **Detention $262.50:** the contract supports $112.50 (see step 5). Variance $150.00, action PARTIAL_APPROVE, reason OVER_CLAIM. The line cites contract clause 4.2 and lists the gate, check-in and release times with their sources.
- **Liftgate $50.00:** the catalog says liftgate requires authorization; CARR-7 has no liftgate row in its contract and no receipt was attached. Raised as UNAUTHORIZED_GHOST_FEE, $0 supported, sent to review.
- **Totals:** claimed $603.30, supported $403.30, variance $200.00. The supported total equals the pre-bill ($403.30): the event-driven proposal and the final result agree.

**What was written to the database by step 7:**

| Table | What was added |
|---|---|
| settlements | The ACTUAL result (all four lines as JSON) with a unique proof ID. |
| audit_records (3 rows) | Fingerprints for linehaul, fuel and detention marked APPROVED. Liftgate was not approved, so no row. |
| invoice_records (1 row) | Carrier CARR-7, invoice INV-1001. |
| fraud_alerts (1 row) | The ghost-fee alert for liftgate. |
| cases (4 rows) | One case per charge, each with owner, next action and starting status. |
| outbox (3 rows) | Messages triggered by the new case statuses (see below). |


### 8. Cases opened for people to work

The cases opened for people to work. Starting status comes from the `action_policies` table using each line's reason code.

**Request:** `GET /api/v1/reconcile/cases?movement_id=DEMO-001`

**Database rows added:** none.

| Charge | Status | Claimed | Supported | Reason |
|---|---|---|---|---|
| LFT | DISPUTE_REVIEW | 50.0 | 0.0 | GHOST_FEE |
| DET | CORRECTION_REQUESTED | 262.5 | 112.5 | OVER_CLAIM |
| FSC | READY_TO_POST | 40.8 | 40.8 | NONE |
| LH | READY_TO_POST | 250.0 | 250.0 | NONE |

Linehaul and fuel start as `READY_TO_POST` (finance can pay). Detention starts as `CORRECTION_REQUESTED` (ask the carrier to correct $150). Liftgate starts as `DISPUTE_REVIEW` (a manager decides).


### 9a. Analyst disputes the detention charge

The analyst disputes the detention charge. The system checks that the role has the dispute permission and that DISPUTE is allowed from CORRECTION_REQUESTED, then writes the audit entry.

**Request:** `POST /api/v1/reconcile/decisions`

```
{
  "movement_id": "DEMO-001",
  "charge_code": "DET",
  "decision": "DISPUTE",
  "reason": "Gate log shows 1.5 h billable, not 3.5 h"
}
```

**Response (HTTP 200):**

```
{
  "movement_id": "DEMO-001",
  "charge_code": "DET",
  "status": "DISPUTED",
  "supported": 112.5
}
```

**Database rows added (table: before -> after):** `audit_log` 0 -> 1, `outbox` 3 -> 4


### 9b. Carrier agrees to the supported amount

The carrier accepts the revised amount. The agreement is recorded; supported becomes the agreed amount.

**Request:** `POST /api/v1/reconcile/decisions`

```
{
  "movement_id": "DEMO-001",
  "charge_code": "DET",
  "decision": "AGREE_SETTLEMENT",
  "amount": 112.5,
  "reason": "Carrier accepted a revised invoice"
}
```

**Response (HTTP 200):**

```
{
  "movement_id": "DEMO-001",
  "charge_code": "DET",
  "status": "AGREED",
  "supported": 112.5
}
```

**Database rows added (table: before -> after):** `audit_log` 1 -> 4, `outbox` 4 -> 5


### 9c. Finance records the payment

Finance records the payment. The system checks the paid amount matches the agreed amount, stores the reference, and marks the original approval record as PAID (which is why a later resubmission says 'already paid').

**Request:** `POST /api/v1/reconcile/decisions`

```
{
  "movement_id": "DEMO-001",
  "charge_code": "DET",
  "decision": "MARK_PAID",
  "amount": 112.5,
  "reference": "PAY-77"
}
```

**Response (HTTP 200):**

```
{
  "movement_id": "DEMO-001",
  "charge_code": "DET",
  "status": "PAID",
  "supported": 112.5
}
```

**Database rows added (table: before -> after):** `audit_log` 4 -> 7, `outbox` 5 -> 6

**The audit trail after these three steps** (every manual change, who, from, to, why):

| Entity | Field | Old | New | Reason | Actor (role) |
|---|---|---|---|---|---|
| DEMO-001:DET | status | CORRECTION_REQUESTED | DISPUTED | Gate log shows 1.5 h billable, not 3.5 h | local-dev (admin) |
| DEMO-001:DET | agreed_amount | - | 112.5 | Carrier accepted a revised invoice | local-dev (admin) |
| DEMO-001:DET | supported | 112.5 | 112.5 | carrier agreement: Carrier accepted a revised invoice | local-dev (admin) |
| DEMO-001:DET | status | DISPUTED | AGREED | Carrier accepted a revised invoice | local-dev (admin) |
| DEMO-001:DET | paid_amount | - | 112.5 | MARK_PAID | local-dev (admin) |
| DEMO-001:DET | audit_record_status | APPROVED | PAID | MARK_PAID | local-dev (admin) |
| DEMO-001:DET | status | AGREED | PAID | MARK_PAID | local-dev (admin) |

**The outbox after these steps** (messages to other systems; in this demo the targets are switched on but nothing is delivered until the dispatcher runs):

| # | Target | Outcome | Status |
|---|---|---|---|
| 1 | finance_erp | SETTLEMENT_READY | PENDING |
| 2 | finance_erp | SETTLEMENT_READY | PENDING |
| 3 | carrier_workflow | CORRECTION_REQUESTED | PENDING |
| 4 | carrier_workflow | DISPUTE_RAISED | PENDING |
| 5 | finance_erp | SETTLEMENT_READY | PENDING |
| 6 | finance_erp | PAYMENT_RECORDED | PENDING |


### 10. Dispute memo for the carrier (PDF)

The dispute memo is a PDF listing every line with variance above zero, with its clause and facts, and any attached documents.

**Request:** `POST /api/v1/reconcile/dispute-memo`

```
{
  "movement_id": "DEMO-001"
}
```

**Response (HTTP 200):**

```
{
  "_binary": true,
  "content_type": "application/pdf",
  "bytes": 3253
}
```

**Database rows added:** none.


### 11. The same bill is sent again (new movement id, no invoice number)

The carrier sends the same bill again, on a new movement id and without an invoice number. Each charge fingerprint already exists, so each is blocked.

**Request:** `POST /api/v1/reconcile/actual`

```
{
  "movement_id": "DEMO-001-RESEND",
  "bol_number": "BOL-1001",
  "carrier_id": "CARR-7",
  "container_or_job_id": "TRL-55",
  "claimed_charges": [
    {
      "charge_code": "LH",
      "amount": 250.0
    },
    {
      "charge_code": "FSC",
      "amount": 40.8
    },
    {
      "charge_code": "DET",
      "amount": 262.5
    },
    {
      "charge_code": "LFT",
      "amount": 50.0
    }
  ],
  "evidence": {
    "fuel": {
      "ship_date": "2025-03-10",
      "miles": 100
    }
  }
}
```

**Database rows added (table: before -> after):** `cases` 4 -> 8, `fraud_alerts` 1 -> 5, `settlements` 3 -> 4

| Charge | Pre-bill | Claimed | Supported | Variance | Action | Reason |
|---|---|---|---|---|---|---|
| LH | - | $250.00 | $0.00 | $250.00 | HITL_REVIEW | DUPLICATE |
| FSC | - | $40.80 | $0.00 | $40.80 | HITL_REVIEW | DUPLICATE |
| DET | - | $262.50 | $0.00 | $262.50 | HITL_REVIEW | DUPLICATE |
| LFT | - | $50.00 | $0.00 | $50.00 | HITL_REVIEW | GHOST_FEE |
| TOTAL | - | $603.30 | $0.00 | $603.30 | HITL_REVIEW |  |

Detention says 'already paid' because step 9c updated the audit record to PAID. Liftgate is flagged again as a ghost fee because it was never approved.


### 12. Same invoice number sent again on a different BOL

The same invoice number arrives on a different BOL. The invoice-number check catches it before any pricing.

**Request:** `POST /api/v1/reconcile/actual`

```
{
  "movement_id": "DEMO-002",
  "bol_number": "BOL-2002",
  "carrier_id": "CARR-7",
  "container_or_job_id": "TRL-55",
  "invoice_number": "INV-1001",
  "claimed_charges": [
    {
      "charge_code": "LH",
      "amount": 250.0
    },
    {
      "charge_code": "FSC",
      "amount": 40.8
    },
    {
      "charge_code": "DET",
      "amount": 262.5
    },
    {
      "charge_code": "LFT",
      "amount": 50.0
    }
  ],
  "evidence": {
    "fuel": {
      "ship_date": "2025-03-10",
      "miles": 100
    }
  }
}
```

**Database rows added (table: before -> after):** `cases` 8 -> 12, `fraud_alerts` 5 -> 6, `settlements` 4 -> 5

| Charge | Pre-bill | Claimed | Supported | Variance | Action | Reason |
|---|---|---|---|---|---|---|
| LH | - | $250.00 | $0.00 | $250.00 | HITL_REVIEW | DUPLICATE_INVOICE |
| FSC | - | $40.80 | $0.00 | $40.80 | HITL_REVIEW | DUPLICATE_INVOICE |
| DET | - | $262.50 | $0.00 | $262.50 | HITL_REVIEW | DUPLICATE_INVOICE |
| LFT | - | $50.00 | $0.00 | $50.00 | HITL_REVIEW | DUPLICATE_INVOICE |
| TOTAL | - | $603.30 | $0.00 | $603.30 | HITL_REVIEW |  |

**Final state of the operational tables after the whole story:**

| Table | Rows |
|---|---|
| audit_log | 7 |
| audit_records | 3 |
| cases | 12 |
| fraud_alerts | 6 |
| invoice_records | 1 |
| movement_events | 4 |
| outbox | 6 |
| settlements | 5 |


# 10. How the 'simulation' and scenarios work

**First, the honest definition:** there is no random or statistical simulation. The scenario buttons replay **prepared sample bills** through the **real** calculation engine. The engine is exactly the one that would process a real bill; only the input is sample data.


### 10.1 Where the scenario data comes from

1. `data/seed/scenarios.json` holds seven stories (A to G). Each is a complete request: carrier, charges claimed and the evidence.
2. At startup the loader copies them into the `scenarios` table.
3. When the dashboard loads, `GET /scenarios` reads that table. The server replaces `{{DATE}}` with today's date, `{{DATE-3}}` with three days earlier, and so on, so the scenarios always look current.
4. The dashboard keeps a copy in the browser. The `{{RUN}}` token in each BOL is replaced with a new unique value on every run, so a re-run is never mistaken for a duplicate (except scenario D, which deliberately sends the same bill twice).


### 10.2 On what basis the results are produced

| Input | Source | Effect |
|---|---|---|
| Contract terms | contracts table (sample carrier CARR-7) | Free time, hourly rate, tiers, clause text. |
| Scenario evidence | scenarios table | The recorded times and dates that are being tested. |
| Claimed amount | scenario, editable on screen | What the carrier billed. |
| Fuel price | fuel_index (live EIA rows once synced) | Fuel surcharge checks. |
| Rules and owners | catalog, action_policies | Fraud flags, owners, next steps. |


### 10.3 What the dashboard does when you interact

| You do this | The browser sends | What the server does | Saved? |
|---|---|---|---|
| Open the page or choose a scenario | GET scenarios, config, decision types | Reads rule tables | No |
| Drag a time marker, tick a day, edit the claim | POST /actual with `dry_run: true` | Runs every check and calculation, then rolls everything back | No |
| Click a scenario button or Submit | POST /actual (not a dry run) | Same calculation, then commits the settlement, fingerprints, cases and outbox rows | Yes |
| Click Dispute, Agree, Paid | POST /decisions | Checks permission and status, updates the case, writes audit entries | Yes |
| Click Dispute memo | POST /dispute-memo | Builds a PDF from the stored settlement | No (file returned) |


### 10.4 The seven scenarios and their expected results

| Scenario | Story | Supported vs claimed | Action |
|---|---|---|---|
| A Clean delivery | Waited 1.5 h, inside the 2 h free time; carrier billed $0. | $0 vs $0 | Approve |
| B Overcharge claim | Waited 3.5 h; carrier billed 3.5 h. | $112.50 vs $262.50 | Partial approve |
| C Missing evidence | No warehouse check-in record. | $0 vs $150.00 | Proof needed |
| D Duplicate fraud | The same bill submitted twice. | $0 vs $112.50 (second) | Needs review |
| E Ocean demurrage | 3 billed days at $250; 2 days had no pickup slots. | $250 + $500 held for review | Needs review |
| F Vessel washing | 4 h package, 5.5 h actual. | $950 vs $950 | Approve |
| G Conflicting evidence | GPS 08:40, driver app 09:05, no check-in. | $0 vs $300.00 | Proof needed |


# 11. Fraud checks and controls

| Check | How it works | Result |
|---|---|---|
| Duplicate claim | SHA-256 fingerprint of carrier + BOL + charge + container/job; matched against approved or paid records. | $0 supported, alert, review |
| Duplicate invoice number | Unique per carrier. | All lines blocked, alert |
| Reference mismatch | Bill's carrier, BOL and container compared with the recorded events. | All lines blocked, alert |
| Ghost fee | Catalog flag requires authorization; no contract row and no receipt. | $0, alert, review |
| Overlapping clocks | Conflicting charge pairs (from the database) with overlapping time windows. | $0, alert, review |
| Fuel peg | Claimed fuel compared with the DOE-pegged amount for that week. | Pegged amount supported, alert |
| Missing proof | Required evidence absent (for example warehouse check-in). | $0, proof requested, nothing guessed |
| Conflicting sources | Sources disagree by more than the tolerance (5 minutes by default). | Most reliable source used, all facts and the conflict shown |
| Unverified terminal hold | Hold claimed without a core record. | Those days held back |
| Unrecognized exception | Claimed exception not in contract or without proof. | Sent to exception review; cause is not treated as a financial finding |
| Unknown charge code | Not in the catalog. | Review |


# 12. Roles, security and audit

| Role | Can do |
|---|---|
| coordinator | View; record events. |
| analyst | Reconcile; record and correct events; accept, dispute, record carrier responses. |
| manager | Everything an analyst does, plus write-offs (including large ones) and amount overrides. |
| finance | View; record payment. |
| admin | Everything, including contracts, roles, calendar, charge catalog. |

- **Authentication:** API keys mapped to a user and role (set in the environment). With authentication off (local mode) you act as an admin.
- **Authorization:** each decision needs a specific permission, and is only allowed from certain case statuses. Write-offs above a configured amount need the large-write-off permission.
- **Audit trail:** every manual change (decision, correction, contract version, role change) records who, role, old value, new value, reason and time. Entries are never edited.
- **Event corrections:** the original event stays, marked superseded; the replacement is linked; an audit entry records the reason.
- **Secrets:** read from the environment or `.env`; the `.env` file is excluded from git. Inbound source secrets are compared in constant time.
- **Tamper evidence:** each stored settlement has a proof ID derived from its content. Documents carry a SHA-256 fingerprint.


# 13. Questions your manager will ask (with answers)


### About the idea

**Q. What problem does this solve in one sentence?**

It checks carrier bills against the contract and recorded evidence and shows exactly how much is justified, why, and who should act, so people stop reconciling by hand with emails and screenshots.

**Q. Who uses it?**

The paying side: freight audit analysts, finance and accounts payable, operations and warehouse managers, carrier relationship managers, and a system administrator. The carrier is the supplier and does not use it.

**Q. Why does it matter financially?**

Detention, demurrage and accessorial charges are easy to inflate or duplicate and hard to verify. Every unsupported dollar caught is a dollar not paid. In the worked example, $200 of a $603.30 bill was challenged automatically.

**Q. Does it replace our TMS, WMS or ERP?**

No. It reads facts from them and sends decisions back. That separation is deliberate.


### About data and the database

**Q. What database do you use and why?**

SQLite for development (a single file, nothing to install), designed to run on PostgreSQL by changing one connection setting. Access is through SQLAlchemy with versioned Alembic migrations. There are 23 tables in three groups: rules, operational data, and control data (audit and outbox).

**Q. Where does the contract data come from?**

Today, from JSON files I wrote from the examples in the product document, loaded into the `contracts` table. In a real deployment contracts would be entered through the admin API (which versions them and audits every change) or loaded from your contract system. We do not read contract PDFs yet.

**Q. How does it know which contract version applies?**

Each contract row has an effective start (and optional end) date. The system uses the version in force on the movement's own date, never today's terms for an old charge. Adding a new version automatically ends the previous one the day before.

**Q. Where do timestamps come from?**

From events. Events enter through the API or the inbound adapters. Today only sample payloads have been used; no real TMS, WMS or GPS feed is connected yet. Each event records its source, original local time, time zone and UTC time.

**Q. What if two systems give different times?**

The most reliable source for that event type (ranking stored in the database) is used for the calculation. All the readings are shown as facts, and a difference above the tolerance is flagged. The system never silently picks the cheaper or more expensive time, and it never replaces a missing warehouse record with GPS or app time.

**Q. How do you handle time zones?**

Naive times are interpreted in the facility's zone, everything is converted to UTC for calculation, and the display shows local time with the UTC value. A test confirms that 09:00 New York and the equivalent explicit offset give the same result.

**Q. Where do fuel prices come from?**

The U.S. EIA weekly retail diesel series, pulled through the free EIA API into the `fuel_index` table (104 weeks loaded). The check does not call the API live; it reads the table, so a bill always gets the same answer.

**Q. How is a duplicate detected?**

A SHA-256 fingerprint of carrier, BOL, charge code and container or job id is stored when a charge is approved. A later claim with the same fingerprint is blocked. Separately, a repeated invoice number and a bill whose references do not match the recorded events are blocked.

**Q. Is the data real?**

No. The contracts, carrier (CARR-7), scenarios and holiday calendar are sample data. The EIA fuel prices are real. The logic is real; the commercial terms are placeholders.


### About the calculations

**Q. Is any AI involved?**

No. All amounts come from stored rules and arithmetic. The original brief mentioned using an LLM to read bill documents; that part is not built.

**Q. How is money calculated accurately?**

Exact decimal arithmetic with half-up rounding to the cent. Supported is capped at the claimed amount.

**Q. Walk me through the detention example.**

Appointment 09:00, truck arrived 08:45 (on time), so the clock starts at 09:00. Released 12:30: 210 minutes. Contract free time 120 minutes leaves 90 billable minutes, which is 1.5 hours at $75, so $112.50. The carrier billed 3.5 hours ($262.50), so the variance is $150.

**Q. What if the truck was late?**

Appointment protection is forfeited and the clock starts at dock start (or completed check-in). If the contract says the clock starts at check-in instead, that rule is used. The rule is data in the contract row.

**Q. What does 'Needs review' mean?**

A person must decide. It is used for fraud alerts, unknown charges, missing contract terms, and amounts that depend on a judgement (for example appointment-constrained days). In the code it is `HITL_REVIEW` (human in the loop).


### About the 'simulation'

**Q. What is the simulation and what is it based on?**

There is no random simulation. The scenarios are prepared sample bills stored in the `scenarios` table. Pressing one sends it through the real engine using the sample contract and the scenario's evidence. See section 10.

**Q. What is the live preview when I drag a marker?**

A dry run: the complete calculation runs and then all database changes are rolled back. Nothing is saved until you press Submit. A test proves a dry run writes nothing and does not hide a real duplicate.

**Q. Are the results the same each time?**

Yes. The calculation has no randomness. Only the run token and dates in the scenarios change.


### About workflow and controls

**Q. What happens after the check?**

A case is opened for each charge with an owner and next action taken from the policy table. People then accept, dispute, record the carrier's reply, agree a settlement, override, write off, or mark paid. Each action is permission-checked and audited.

**Q. Who can write off money?**

Managers and admins. Write-offs above a configured amount need the large-write-off permission. Every write-off needs a reason.

**Q. What gets sent to other systems?**

Settlement-ready, correction-requested, dispute-raised and payment-recorded messages, and prevention alerts, via an outbox table. If a downstream system is down the settlement is not lost; delivery retries. Targets are disabled until real endpoints are configured.

**Q. Can you prove who changed what?**

Yes. The audit log records user, role, old value, new value, reason and time for every manual change, including contract versions and role changes.

**Q. How do you prevent problems before a bill arrives?**

The prevention check looks at open truck movements and warns when free time is running out or exhausted, or when nothing has arrived after the appointment. It can queue the alert to the TMS target.


### About quality and readiness

**Q. How was it tested?**

50 automated tests (about 92% coverage) cover the product document's three examples, the fraud checks, time zones, events, decisions and permissions, the outbox with a mock server, holds, exceptions, calendar, admin changes, documents and the dry-run behaviour. The dashboard was exercised in a real browser. There are no load or security penetration tests yet.

**Q. Is it production ready?**

It is a strong proof of concept with production-style foundations (database, migrations, security, audit, CI). To go live it needs real system connections, real contract data, single sign-on, monitoring, backups, performance testing and a hosted PostgreSQL.

**Q. How hard is it to add a new carrier?**

Add that carrier's rows to the contracts table, through the admin API or the JSON file. No code changes.

**Q. How hard is it to add a new type of charge?**

If it fits an existing pattern (flat fee, per-day with free days, hourly after free time), add a catalog row and a contract row. A genuinely new calculation needs a new calculator function.

**Q. What does it cost to run?**

Nothing in licenses. Everything is open source: Python, FastAPI, SQLAlchemy, SQLite or PostgreSQL, ReportLab. The EIA API is free.

**Q. Does it work on Windows without Docker?**

Yes. `scripts/run.ps1` migrates the database, loads rules, refreshes fuel prices and starts the server in a single process (Windows cannot share a port across worker processes).

**Q. Is personal data stored?**

Only user names in the audit log and uploaded documents' metadata. The system processes shipment and charge data, not customer or employee records.

**Q. What is the 'proof pack'?**

An honest description: each stored settlement has a unique proof ID derived from its content, the full result is stored as JSON, and attached documents carry SHA-256 fingerprints. The dispute memo PDF lists the evidence. It is not a signed or zipped bundle.


# 14. Honest limitations and what comes next

| Limitation | Why it matters | Next step |
|---|---|---|
| No real system is connected | Events and results move through the API and sample payloads only. | Connect real TMS, WMS, terminal and ERP feeds using the adapter and outbox layer. |
| Contracts are sample data for one carrier | Results prove the logic, not your actual commercial terms. | Load real carrier agreements; consider reading contract documents. |
| Bills arrive as structured data | A real carrier invoice is a PDF or EDI file. | Add document reading (the original brief proposed an LLM for this step) and EDI intake. |
| Authentication is shared API keys | No per-user login or single sign-on. | Integrate your company's identity provider. |
| SQLite and a single process on Windows | Fine for a demo; not for many users. | Deploy on PostgreSQL with several workers on Linux. |
| Fuel price is not checked for age | If the sync stops, the latest saved week is used silently. | Add a staleness warning. |
| Overlap check needs the time windows supplied | It does not infer a layover from events. | Derive claim windows from events automatically. |
| Ocean and rail evidence is submitted with the bill | Only truck and service events come through the adapters. | Add terminal and rail event mappings. |
| No load or penetration testing | Behaviour at scale is unknown. | Performance and security testing before production. |
| Prevention only covers truck detention | Open service jobs and container clocks do not raise alerts. | Extend to other clocks. |


# 15. How to run, demo and test


### 15.1 Run

```

cd "C:\Users\devas\OneDrive - Nous Infosystems\Documents\vectura"
pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts/run.ps1
```

Open http://localhost:8000. The script creates the tables, loads the rules, refreshes fuel prices if an EIA key is set in `.env`, and starts the server. Stop it with Ctrl+C. The interactive API documentation is at http://localhost:8000/docs.


### 15.2 Test

```

pip install -r requirements-dev.txt
pytest --cov=src/vectura
ruff check src tests
```


### 15.3 A 5-minute demo order

1. Open Settlements. Click scenario B (Overcharge claim). Point to claimed, supported and variance, then expand the row to show the evidence facts and the contract clause.
2. Drag the release marker earlier. The supported amount drops live, labelled 'Preview, not saved'.
3. Click C and G to show it refuses to guess when proof is missing or sources disagree.
4. Click D for the duplicate alert. Switch to Ocean (E) and Service (F).
5. Submit B, press Dispute (with a reason), Agree settlement, Mark paid. Show the audit trail and the dispute memo PDF.
6. Open Prevention and use the Event lab: fill the example and send events.
7. Open Operations to show integrations, the outbound queue and the document drop zone.
8. Run `pytest` and show the passing tests.


### 15.4 Where to change things

| I want to change | Edit |
|---|---|
| A rate, free time or clause | data/seed/contracts.json (or the admin API) |
| Which charges need authorization | data/seed/catalog.json |
| Who owns a problem and what the next step is | data/seed/policy.json (action_policies) |
| Who may approve or write off | data/seed/policy.json (role_permissions) |
| Which source's time wins | data/seed/events.json (source_reliability) |
| A holiday | data/seed/calendar.json |
| The demo scenarios | data/seed/scenarios.json |
| Ports, keys, tolerances | .env or environment variables (VECTURA_...) |


## Appendix: the web endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | / |  |
| POST | /api/v1/reconcile/actual | Stage 2: final settlement from operational evidence and the carrier's claimed bill. |
| POST | /api/v1/reconcile/admin/calendar/{location} |  |
| PUT | /api/v1/reconcile/admin/charges/{code} |  |
| GET | /api/v1/reconcile/admin/contracts |  |
| POST | /api/v1/reconcile/admin/contracts | New contract version from a date; the previous open one ends the day before. Audited. |
| PUT | /api/v1/reconcile/admin/roles/{role} |  |
| PUT | /api/v1/reconcile/admin/source-reliability/{event_type}/{source} |  |
| GET | /api/v1/reconcile/audit-log |  |
| GET | /api/v1/reconcile/cases |  |
| GET | /api/v1/reconcile/config |  |
| GET | /api/v1/reconcile/decision-types |  |
| POST | /api/v1/reconcile/decisions | Accept / dispute / carrier response / write-off / override / mark paid, permission-checked and audited. |
| POST | /api/v1/reconcile/dispute-memo | PDF memo for the latest ACTUAL settlement of a movement (lines with variance > 0). |
| POST | /api/v1/reconcile/documents |  |
| GET | /api/v1/reconcile/documents |  |
| GET | /api/v1/reconcile/documents/{doc_id}/download |  |
| GET | /api/v1/reconcile/event-types | Event types and their acceptable sources (ranked), so clients need no hardcoded lists. |
| POST | /api/v1/reconcile/events | Record gate / check-in / sign-off events. A clock-closing event auto-prices a pre-bill proposal. |
| POST | /api/v1/reconcile/events/{event_id}/correct |  |
| GET | /api/v1/reconcile/events/{movement_id} |  |
| GET | /api/v1/reconcile/fraud-alerts |  |
| GET | /api/v1/reconcile/integrations |  |
| POST | /api/v1/reconcile/integrations/{source}/events | Inbound adapter: a source system (TMS, WMS, GPS...) posts its own payload; the stored mapping translates it. |
| GET | /api/v1/reconcile/outbox |  |
| POST | /api/v1/reconcile/outbox/dispatch | Deliver pending outbound messages now (the same call a scheduled job makes). |
| GET | /api/v1/reconcile/prevention | Open movements whose free time is running out or exceeded, or with no arrival past the appointment. |
| POST | /api/v1/reconcile/provisional | Stage 1: Pro-Forma freight invoice at origin departure. |
| GET | /api/v1/reconcile/reports/repeat-causes | Recurring causes of charges by facility / terminal, repeated ones first. |
| POST | /api/v1/reconcile/reset | Dev/demo only (VECTURA_ALLOW_RESET=true): wipes audit history. |
| GET | /api/v1/reconcile/scenarios |  |
| GET | /healthz |  |
| GET | /readyz |  |
