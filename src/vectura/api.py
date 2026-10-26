"""FastAPI app for Reconcile V3. Run: uvicorn vectura.api:app --app-dir src"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from .admin import (
    ContractIn,
    add_non_working_days,
    new_contract_version,
    save_document,
    set_role_permissions,
    set_source_rank,
    upsert_charge,
)
from .charge_clock_engine import ReconciliationResult
from .config import Settings, get_settings
from .db import get_session
from .decisions import decide
from .events import closes_clock, correct, ingest, prevention_alerts, recorded_evidence
from .integrations import authenticate_source, dispatch, enqueue, normalize
from .models import (
    AuditLog,
    AuditRecord,
    Case,
    ChargeCatalog,
    Contract,
    DecisionType,
    Document,
    EventType,
    FraudAlertRow,
    IntegrationSource,
    IntegrationTarget,
    MovementEvent,
    OutboxMessage,
    Scenario,
    Settlement,
    SourceReliability,
)
from .pdf_memo import build_memo
from .reports import repeat_causes
from .rule_engine import Reconciler
from .schemas import (
    ActualRequest,
    CorrectionRequest,
    DecisionRequest,
    EventBatch,
    MemoRequest,
    ProvisionalRequest,
)
from .security import Principal, get_principal, require_permission

PREFIX = "/api/v1/reconcile"
log = logging.getLogger("vectura")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {"ts": self.formatTime(record), "level": record.levelname, "logger": record.name,
                "msg": record.getMessage()}
        base.update({k: v for k, v in record.__dict__.items()
                     if k not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__ and k != "message"})
        return json.dumps(base, default=str)


def configure_logging(cfg: Settings) -> None:
    h = logging.StreamHandler()
    if cfg.log_json:
        h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(cfg.log_level)


class RolePerms(BaseModel):
    permissions: List[str]
    reason: str = Field(min_length=3)


class SourceRank(BaseModel):
    rank: int = Field(ge=1)
    reason: str = Field(min_length=3)


class CalendarIn(BaseModel):
    days: Dict[date, str]
    reason: str = Field(min_length=3)


class ChargeIn(BaseModel):
    description: str
    requires_authorization: bool
    reason: str = Field(min_length=3)


def create_app() -> FastAPI:
    cfg = get_settings()
    configure_logging(cfg)
    app = FastAPI(title="Vectura Reconcile V3", version="3.0.0")
    if cfg.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        rid, t0 = request.headers.get("x-request-id", uuid.uuid4().hex), time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        log.info("request", extra={"rid": rid, "method": request.method, "path": request.url.path,
                                   "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1)})
        return response

    @app.exception_handler(PermissionError)
    async def permission_error(_, exc: PermissionError):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_error(_, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(LookupError)
    async def lookup_error(_, exc: LookupError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    # ---------------- ops ---------------- #
    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(s: Session = Depends(get_session)):
        s.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/", include_in_schema=False)
    def index(c: Settings = Depends(get_settings)):
        return FileResponse(c.frontend_dir / "index.html")

    view = [Depends(require_permission("view"))]

    @app.get(f"{PREFIX}/config", dependencies=view)
    def ui_config(c: Settings = Depends(get_settings)):
        return {"day_start": c.ui_day_start_minutes, "day_end": c.ui_day_end_minutes,
                "step": c.ui_slider_step_minutes, "timezone": c.default_timezone, "allow_reset": c.allow_reset, "auth_enabled": c.auth_enabled}

    @app.get(f"{PREFIX}/scenarios", dependencies=view)
    def scenarios(s: Session = Depends(get_session)):
        today = date.today()
        descriptions = {c.code: c.description for c in s.query(ChargeCatalog).all()}
        out = []
        for sc in s.query(Scenario).order_by(Scenario.sort_order).all():
            blob = re.sub(r"\{\{DATE([+-]\d+)?\}\}",
                          lambda m: (today + timedelta(days=int(m.group(1) or 0))).isoformat(), json.dumps(sc.request))
            req = json.loads(blob)
            first = req["claimed_charges"][0]["charge_code"]
            out.append({"name": sc.name, "label": sc.label, "description": sc.description, "mode": sc.mode,
                        "mode_label": descriptions.get(first, sc.mode), "repeat": sc.repeat, "request": req})
        return out

    # ---------------- reconcile ---------------- #
    @app.post(f"{PREFIX}/provisional", response_model=ReconciliationResult, dependencies=[Depends(require_permission("reconcile"))])
    def provisional(req: ProvisionalRequest, s: Session = Depends(get_session)):
        """Stage 1: Pro-Forma freight invoice at origin departure."""
        return Reconciler(s).provisional(req)

    @app.post(f"{PREFIX}/actual", response_model=ReconciliationResult, dependencies=[Depends(require_permission("reconcile"))])
    def actual(req: ActualRequest, s: Session = Depends(get_session)):
        """Stage 2: final settlement from operational evidence and the carrier's claimed bill."""
        return Reconciler(s).actual(req)

    @app.post(f"{PREFIX}/dispute-memo", dependencies=view)
    def dispute_memo(req: MemoRequest, s: Session = Depends(get_session)):
        """PDF memo for the latest ACTUAL settlement of a movement (lines with variance > 0)."""
        row = (s.query(Settlement).filter_by(movement_id=req.movement_id, stage="ACTUAL")
               .order_by(Settlement.id.desc()).first())
        if not row:
            raise HTTPException(404, "No ACTUAL settlement for this movement")
        result = ReconciliationResult(**row.payload)
        if not any(li.variance > 0 for li in result.line_items):
            raise HTTPException(422, "No line items with variance > 0; nothing to dispute.")
        docs = [{"kind": d.kind, "filename": d.filename, "sha256": d.sha256, "size_bytes": d.size_bytes,
                 "uploaded_by": d.uploaded_by} for d in s.query(Document).filter_by(movement_id=req.movement_id).all()]
        return Response(build_memo(result, docs), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="dispute-{req.movement_id}.pdf"'})

    @app.get(f"{PREFIX}/fraud-alerts", dependencies=view)
    def fraud_alerts(alert_type: Optional[str] = None, movement_id: Optional[str] = None,
                     limit: Optional[int] = Query(None, ge=1), offset: int = Query(0, ge=0),
                     s: Session = Depends(get_session), c: Settings = Depends(get_settings)):
        q = s.query(FraudAlertRow)
        if alert_type:
            q = q.filter_by(alert_type=alert_type)
        if movement_id:
            q = q.filter_by(movement_id=movement_id)
        lim = min(limit or c.default_page_size, c.max_page_size)
        rows = q.order_by(FraudAlertRow.id.desc()).offset(offset).limit(lim).all()
        return [{"id": r.id, "movement_id": r.movement_id, "alert_type": r.alert_type, "charge_code": r.charge_code,
                 "detail": r.detail, "created_at": r.created_at.isoformat()} for r in rows]

    @app.post(f"{PREFIX}/reset", include_in_schema=False, dependencies=[Depends(require_permission("admin"))])
    def reset(s: Session = Depends(get_session), c: Settings = Depends(get_settings)):
        """Dev/demo only (VECTURA_ALLOW_RESET=true): wipes audit history."""
        if not c.allow_reset or c.environment == "production":
            raise HTTPException(403, "Reset disabled")
        for m in (FraudAlertRow, AuditRecord, Settlement):
            s.query(m).delete()
        s.commit()
        return {"ok": True}

    # ---------------- events, prevention, cases, decisions, audit ---------------- #
    @app.post(f"{PREFIX}/events")
    def post_events(batch: EventBatch, s: Session = Depends(get_session),
                    who: Principal = Depends(require_permission("ingest_events"))):
        """Record gate / check-in / sign-off events. A clock-closing event auto-prices a pre-bill proposal."""
        rows = ingest(s, batch, who.user)
        proposal = None
        if closes_clock(s, rows):
            proposal = Reconciler(s).propose(batch.movement_id, batch.carrier_id, batch.container_or_job_id)
        s.commit()
        return {"event_ids": [r.id for r in rows], "proposal": proposal}

    @app.get(f"{PREFIX}/events/{{movement_id}}", dependencies=view)
    def get_events(movement_id: str, s: Session = Depends(get_session)):
        rec = recorded_evidence(s, movement_id)
        rows = s.query(MovementEvent).filter_by(movement_id=movement_id).order_by(MovementEvent.id).all()
        return {"events": [{"id": r.id, "event_type": r.event_type, "source": r.source, "occurred_at_utc": r.occurred_at.isoformat(),
                            "as_received": r.original_value, "recorded_by": r.recorded_by,
                            "superseded_by": r.superseded_by} for r in rows],
                "facts": rec.facts, "conflicts": rec.conflicts}

    @app.post(f"{PREFIX}/events/{{event_id}}/correct")
    def correct_event(event_id: int, req: CorrectionRequest, s: Session = Depends(get_session),
                      who: Principal = Depends(require_permission("correct_events"))):
        new = correct(s, event_id, req, who.user, who.role)
        s.commit()
        return {"new_event_id": new.id, "occurred_at_utc": new.occurred_at.isoformat()}

    @app.get(f"{PREFIX}/prevention", dependencies=view)
    def prevention(as_of: Optional[datetime] = None, notify: bool = False, s: Session = Depends(get_session)):
        """Open movements whose free time is running out or exceeded, or with no arrival past the appointment.

        notify=true also queues each alert for the subscribed TMS / operations target."""
        alerts = prevention_alerts(s, as_of)
        if notify:
            for a in alerts:
                enqueue(s, "PREVENTION_ALERT", a["movement_id"], a)
            s.commit()
        return alerts

    @app.get(f"{PREFIX}/cases", dependencies=view)
    def cases(status: Optional[str] = None, owner: Optional[str] = None, movement_id: Optional[str] = None,
              limit: Optional[int] = Query(None, ge=1), offset: int = Query(0, ge=0),
              s: Session = Depends(get_session), c: Settings = Depends(get_settings)):
        q = s.query(Case)
        if status:
            q = q.filter_by(status=status)
        if owner:
            q = q.filter(Case.decision_owner.contains(owner))
        if movement_id:
            q = q.filter_by(movement_id=movement_id)
        rows = q.order_by(Case.id.desc()).offset(offset).limit(min(limit or c.default_page_size, c.max_page_size)).all()
        return [{col.name: getattr(r, col.name) for col in Case.__table__.columns if col.name != "claim_hash"}
                | {"updated_at": r.updated_at.isoformat()} for r in rows]

    @app.get(f"{PREFIX}/event-types", dependencies=view)
    def event_types(s: Session = Depends(get_session)):
        """Event types and their acceptable sources (ranked), so clients need no hardcoded lists."""
        ranks: Dict[str, list] = {}
        for r in s.query(SourceReliability).order_by(SourceReliability.rank).all():
            ranks.setdefault(r.event_type, []).append({"source": r.source, "rank": r.rank})
        return [{"code": t.code, "description": t.description, "group": t.evidence_group,
                 "closes_clock": t.closes_clock, "sources": ranks.get(t.code, [])} for t in s.query(EventType).all()]

    @app.get(f"{PREFIX}/decision-types", dependencies=view)
    def decision_types(s: Session = Depends(get_session)):
        return [{"code": d.code, "requires_reason": d.requires_reason, "needs_amount": d.needs_amount, "needs_reference": d.needs_reference,
                 "allowed_from": d.allowed_from} for d in s.query(DecisionType).all()]

    @app.post(f"{PREFIX}/decisions")
    def decisions(req: DecisionRequest, s: Session = Depends(get_session), who: Principal = Depends(get_principal)):
        """Accept / dispute / carrier response / write-off / override / mark paid, permission-checked and audited."""
        case = decide(s, req, who)
        return {"movement_id": case.movement_id, "charge_code": case.charge_code, "status": case.status,
                "supported": case.supported}

    @app.get(f"{PREFIX}/audit-log", dependencies=view)
    def audit_log(entity_id: Optional[str] = None, limit: Optional[int] = Query(None, ge=1),
                  s: Session = Depends(get_session), c: Settings = Depends(get_settings)):
        q = s.query(AuditLog)
        if entity_id:
            q = q.filter_by(entity_id=entity_id)
        rows = q.order_by(AuditLog.id.desc()).limit(min(limit or c.default_page_size, c.max_page_size)).all()
        return [{"id": r.id, "entity": r.entity, "entity_id": r.entity_id, "field": r.field, "old": r.old_value,
                 "new": r.new_value, "reason": r.reason, "actor": r.actor, "role": r.role,
                 "at": r.created_at.isoformat()} for r in rows]

    # ---------------- integrations ---------------- #
    @app.post(f"{PREFIX}/integrations/{{source}}/events")
    def inbound(source: str, payload: dict, x_source_secret: Optional[str] = Header(default=None),
                s: Session = Depends(get_session), c: Settings = Depends(get_settings)):
        """Inbound adapter: a source system (TMS, WMS, GPS...) posts its own payload; the stored mapping translates it."""
        src = s.get(IntegrationSource, source)
        if src is None or not src.enabled:
            raise HTTPException(404, "Unknown or disabled source")
        authenticate_source(src, x_source_secret, c)
        batch, skipped = normalize(src, payload)
        rows = ingest(s, batch, f"source:{source}")
        proposal = None
        if closes_clock(s, rows):
            proposal = Reconciler(s).propose(batch.movement_id, batch.carrier_id, batch.container_or_job_id)
        s.commit()
        return {"accepted": len(rows), "skipped_unmapped": skipped, "proposal": proposal}

    @app.get(f"{PREFIX}/integrations", dependencies=view)
    def integrations(s: Session = Depends(get_session)):
        return {"sources": [{"name": r.name, "description": r.description, "enabled": r.enabled} for r in s.query(IntegrationSource).all()],
                "targets": [{"name": t.name, "description": t.description, "subscribes": t.subscribes, "enabled": t.enabled}
                            for t in s.query(IntegrationTarget).all()]}

    @app.get(f"{PREFIX}/outbox", dependencies=view)
    def outbox(status: Optional[str] = None, limit: Optional[int] = Query(None, ge=1), s: Session = Depends(get_session),
               c: Settings = Depends(get_settings)):
        q = s.query(OutboxMessage)
        if status:
            q = q.filter_by(status=status)
        rows = q.order_by(OutboxMessage.id.desc()).limit(min(limit or c.default_page_size, c.max_page_size)).all()
        return [{"id": r.id, "target": r.target, "kind": r.kind, "movement_id": r.movement_id, "status": r.status,
                 "attempts": r.attempts, "last_error": r.last_error} for r in rows]

    @app.post(f"{PREFIX}/outbox/dispatch", dependencies=[Depends(require_permission("admin"))])
    def outbox_dispatch(s: Session = Depends(get_session)):
        """Deliver pending outbound messages now (the same call a scheduled job makes)."""
        return dispatch(s)

    # ---------------- documents (evidence attachments) ---------------- #
    @app.post(f"{PREFIX}/documents")
    async def upload_document(movement_id: str, kind: str, file: UploadFile, s: Session = Depends(get_session),
                              who: Principal = Depends(require_permission("ingest_events")), c: Settings = Depends(get_settings)):
        data = await file.read(c.max_document_bytes + 1)
        doc = save_document(s, movement_id, kind, file.filename or "document", data, who, c)
        return {"id": doc.id, "sha256": doc.sha256, "size_bytes": doc.size_bytes}

    @app.get(f"{PREFIX}/documents", dependencies=view)
    def list_documents(movement_id: str, s: Session = Depends(get_session)):
        return [{"id": d.id, "kind": d.kind, "filename": d.filename, "sha256": d.sha256, "size_bytes": d.size_bytes,
                 "uploaded_by": d.uploaded_by} for d in s.query(Document).filter_by(movement_id=movement_id).all()]

    @app.get(f"{PREFIX}/documents/{{doc_id}}/download", dependencies=view)
    def download_document(doc_id: int, s: Session = Depends(get_session)):
        d = s.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "Document not found")
        return FileResponse(d.storage_path, filename=d.filename)

    # ---------------- manager report ---------------- #
    @app.get(f"{PREFIX}/reports/repeat-causes", dependencies=view)
    def repeat_causes_report(location: Optional[str] = None, s: Session = Depends(get_session)):
        """Recurring causes of charges by facility / terminal, repeated ones first."""
        return repeat_causes(s, location)

    # ---------------- system administrator ---------------- #
    adm = [Depends(require_permission("admin"))]

    @app.get(f"{PREFIX}/admin/contracts", dependencies=adm)
    def admin_contracts(carrier_id: Optional[str] = None, s: Session = Depends(get_session)):
        q = s.query(Contract)
        if carrier_id:
            q = q.filter_by(carrier_id=carrier_id)
        return [{"id": r.id, "carrier_id": r.carrier_id, "charge_code": r.charge_code, "effective_from": str(r.effective_from),
                 "effective_to": str(r.effective_to) if r.effective_to else None, "terms": r.terms,
                 "clause_ref": r.clause_ref} for r in q.order_by(Contract.carrier_id, Contract.charge_code, Contract.effective_from).all()]

    @app.post(f"{PREFIX}/admin/contracts", dependencies=adm)
    def admin_new_contract(body: ContractIn, s: Session = Depends(get_session), who: Principal = Depends(get_principal)):
        """New contract version from a date; the previous open one ends the day before. Audited."""
        row = new_contract_version(s, body, who)
        return {"id": row.id}

    @app.put(f"{PREFIX}/admin/roles/{{role}}", dependencies=adm)
    def admin_role(role: str, body: RolePerms, s: Session = Depends(get_session), who: Principal = Depends(get_principal)):
        set_role_permissions(s, role, body.permissions, body.reason, who)
        return {"role": role, "permissions": sorted(set(body.permissions))}

    @app.put(f"{PREFIX}/admin/source-reliability/{{event_type}}/{{source}}", dependencies=adm)
    def admin_source_rank(event_type: str, source: str, body: SourceRank, s: Session = Depends(get_session),
                          who: Principal = Depends(get_principal)):
        set_source_rank(s, event_type, source, body.rank, body.reason, who)
        return {"event_type": event_type, "source": source, "rank": body.rank}

    @app.post(f"{PREFIX}/admin/calendar/{{location}}", dependencies=adm)
    def admin_calendar(location: str, body: CalendarIn, s: Session = Depends(get_session), who: Principal = Depends(get_principal)):
        return {"added": add_non_working_days(s, location, body.days, body.reason, who)}

    @app.put(f"{PREFIX}/admin/charges/{{code}}", dependencies=adm)
    def admin_charge(code: str, body: ChargeIn, s: Session = Depends(get_session), who: Principal = Depends(get_principal)):
        upsert_charge(s, code, body.description, body.requires_authorization, body.reason, who)
        return {"code": code}

    return app


app = create_app()
