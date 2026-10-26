"""Movement event store: ingest, correct (with audit trail), assemble clock evidence, prevention alerts.

Reliability ranks and event-to-evidence mapping live in the DB (event_types, source_reliability).
Original local time, zone, source and later corrections are all kept; clocks run on UTC.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import ActionPolicy, AuditLog, Contract, EventType, MovementEvent, SourceReliability
from .schemas import CorrectionRequest, EventBatch, to_utc


@dataclass
class RecordedEvidence:
    groups: Dict[str, Dict[str, datetime]] = field(default_factory=dict)  # group -> evidence_field -> UTC time
    facts: Dict[str, List[str]] = field(default_factory=dict)
    conflicts: List[str] = field(default_factory=list)
    meta: Optional[MovementEvent] = None


def as_utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes for stored UTC values; PostgreSQL returns aware ones."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def fmt_fact(label: str, source: str, utc: datetime, tz: str) -> str:
    local = utc.astimezone(ZoneInfo(tz))
    return f"{label} · {source} · {local:%Y-%m-%d %H:%M %Z} ({utc:%H:%M}Z)"


def ingest(session: Session, batch: EventBatch, actor: str, cfg: Optional[Settings] = None) -> List[MovementEvent]:
    cfg = cfg or get_settings()
    types = {t.code for t in session.query(EventType).all()}
    ranks = {(r.event_type, r.source) for r in session.query(SourceReliability).all()}
    out = []
    for e in batch.events:
        if e.event_type not in types:
            raise ValueError(f"Unknown event type {e.event_type}")
        if (e.event_type, e.source) not in ranks:
            raise ValueError(f"Source {e.source} is not a recognized source for {e.event_type}")
        tz = e.timezone or cfg.default_timezone
        row = MovementEvent(
            movement_id=batch.movement_id, carrier_id=batch.carrier_id, bol_number=batch.bol_number,
            container_or_job_id=batch.container_or_job_id, event_type=e.event_type,
            occurred_at=to_utc(e.occurred_at, tz), original_value=f"{e.occurred_at.isoformat()} [{tz}]",
            local_tz=tz, source=e.source, recorded_by=actor, location=batch.location)
        session.add(row)
        out.append(row)
    session.flush()
    return out


def correct(session: Session, event_id: int, req: CorrectionRequest, actor: str, role: str,
            cfg: Optional[Settings] = None) -> MovementEvent:
    """Replace an event's time; the original stays, flagged superseded, with an audit entry."""
    cfg = cfg or get_settings()
    old = session.get(MovementEvent, event_id)
    if old is None or old.superseded_by is not None:
        raise LookupError("Event not found or already superseded")
    tz = req.timezone or old.local_tz or cfg.default_timezone
    new = MovementEvent(
        movement_id=old.movement_id, carrier_id=old.carrier_id, bol_number=old.bol_number,
        container_or_job_id=old.container_or_job_id, event_type=old.event_type,
        occurred_at=to_utc(req.new_occurred_at, tz), original_value=f"{req.new_occurred_at.isoformat()} [{tz}]",
        local_tz=tz, source=old.source, recorded_by=actor, location=old.location)
    session.add(new)
    session.flush()
    old.superseded_by = new.id
    session.add(AuditLog(entity="movement_event", entity_id=str(old.id), field="occurred_at",
                         old_value=old.original_value, new_value=new.original_value,
                         reason=req.reason, actor=actor, role=role))
    return new


def recorded_evidence(session: Session, movement_id: str, cfg: Optional[Settings] = None) -> RecordedEvidence:
    """Pick, per event type, the most reliable source; list every fact and any source conflicts."""
    cfg = cfg or get_settings()
    types = {t.code: t for t in session.query(EventType).all()}
    rank = {(r.event_type, r.source): r.rank for r in session.query(SourceReliability).all()}
    events = (session.query(MovementEvent)
              .filter_by(movement_id=movement_id, superseded_by=None).order_by(MovementEvent.occurred_at).all())
    rec = RecordedEvidence(meta=events[0] if events else None)
    by_type: Dict[str, List[MovementEvent]] = defaultdict(list)
    for ev in events:
        ev.occurred_at = as_utc(ev.occurred_at)
        by_type[ev.event_type].append(ev)
    for code, evs in by_type.items():
        t = types[code]
        best = min(evs, key=lambda x: rank.get((code, x.source), 10**6))
        rec.groups.setdefault(t.evidence_group, {})[t.evidence_field] = best.occurred_at
        rec.facts.setdefault(t.evidence_group, []).extend(
            fmt_fact(t.description, x.source, x.occurred_at, x.local_tz) for x in evs)
        if len(evs) > 1:
            spread = (max(x.occurred_at for x in evs) - min(x.occurred_at for x in evs)).total_seconds() / 60
            if spread > cfg.conflict_tolerance_minutes:
                rec.conflicts.append(f"{t.description}: sources disagree by {spread:.0f} min; using {best.source}.")
    return rec


def closes_clock(session: Session, events: List[MovementEvent]) -> bool:
    closing = {t.code for t in session.query(EventType).filter_by(closes_clock=True).all()}
    return any(e.event_type in closing for e in events)


def prevention_alerts(session: Session, as_of: Optional[datetime] = None, cfg: Optional[Settings] = None) -> List[dict]:
    """Open TL movements whose free time is running out / exceeded, or with no arrival past the appointment."""
    cfg = cfg or get_settings()
    now = as_of or datetime.now(timezone.utc)
    policies = {p.reason_code: p for p in session.query(ActionPolicy).all()}
    out: List[dict] = []
    for (mid,) in session.query(MovementEvent.movement_id).distinct().all():
        rec = recorded_evidence(session, mid, cfg)
        tl, m = rec.groups.get("tl", {}), rec.meta
        if m is None or "release_time" in tl:
            continue  # nothing recorded, or the clock is already closed
        terms = _det_terms(session, m.carrier_id, now.date())
        if terms is None:
            continue

        def alert(code: str, detail: str, remaining: Optional[float] = None, mid=mid, m=m):
            p = policies[code]
            out.append({"movement_id": mid, "carrier_id": m.carrier_id, "alert_type": code, "detail": detail,
                        "minutes_remaining": remaining, "decision_owner": p.decision_owner, "next_action": p.next_action})

        appt, arr = tl.get("appointment_time"), tl.get("arrival_time")
        if appt and not arr and now > appt:
            alert("PREVENT_NO_ARRIVAL", f"Appointment {appt:%Y-%m-%d %H:%MZ} passed with no arrival recorded.")
            continue
        start = _clock_start(terms["start_rule"], tl)
        if start is None:
            continue
        remaining = terms["free_time_minutes"] - (now - start).total_seconds() / 60
        if remaining <= 0:
            alert("PREVENT_EXCEEDED", f"Free time exhausted {abs(remaining):.0f} min ago (clock start {start:%H:%MZ}).", remaining)
        elif remaining <= cfg.prevention_warn_minutes:
            alert("PREVENT_APPROACHING", f"{remaining:.0f} min of free time left (clock start {start:%H:%MZ}).", remaining)
    return out


def _det_terms(session: Session, carrier: str, on: date) -> Optional[dict]:
    row = (session.query(Contract).filter(Contract.carrier_id == carrier, Contract.charge_code == "DET",
                                          Contract.effective_from <= on).order_by(Contract.effective_from.desc()).first())
    return row.terms if row else None


def _clock_start(rule: str, tl: Dict[str, datetime]) -> Optional[datetime]:
    if rule == "CHECK_IN":
        return tl.get("check_in_time")
    if rule == "DOCK_START":
        return tl.get("dock_start_time")
    appt, arr = tl.get("appointment_time"), tl.get("arrival_time")
    if not (appt and arr and tl.get("check_in_time")):
        return None
    return appt if arr <= appt else (tl.get("dock_start_time") or tl["check_in_time"])
