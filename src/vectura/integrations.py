"""Integration layer: config-driven inbound adapters and a transactional outbox for outbound delivery.

Inbound: each source system's payload shape is described by a mapping row in `integration_sources`
(no code per system). Outbound: results are written to `outbox` in the same transaction as the business
change and delivered by a dispatcher with retries (`python -m vectura.integrations`), so a failed
downstream call never loses or blocks a settlement.
"""
from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import Case, IntegrationSource, IntegrationTarget, OutboxMessage, StatusOutcome
from .schemas import EventBatch, EventIn

log = logging.getLogger(__name__)


def _get(payload: Dict[str, Any], path: Optional[str]) -> Any:
    cur: Any = payload
    for part in (path or "").split("."):
        if not part:
            return None
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


# ----------------------------- inbound ----------------------------- #
def authenticate_source(src: IntegrationSource, supplied: Optional[str], cfg: Settings) -> None:
    if not cfg.auth_enabled or not src.auth_env:
        return
    secret = os.environ.get(src.auth_env, "")
    if not secret or not supplied or not hmac.compare_digest(secret, supplied):
        raise PermissionError(f"Invalid or missing secret for source '{src.name}'")


def normalize(src: IntegrationSource, payload: Dict[str, Any]) -> tuple[EventBatch, List[str]]:
    """Translate a source payload into a movement-event batch; unmapped event codes are reported, not guessed."""
    m = src.mapping
    movement = _get(payload, m["movement_field"])
    if not movement:
        raise ValueError(f"Payload has no '{m['movement_field']}'")
    events, skipped = [], []
    for raw in _get(payload, m["events_field"]) or []:
        code = _get(raw, m["type_field"])
        target = m["types"].get(code)
        if not target:
            skipped.append(str(code))
            continue
        ts = _get(raw, m["time_field"])
        if ts is None:
            raise ValueError(f"Event {code} has no '{m['time_field']}'")
        events.append(EventIn(event_type=target[0], source=target[1], occurred_at=ts,
                              timezone=_get(raw, m.get("tz_field")) or _get(payload, m.get("tz_field"))))
    batch = EventBatch(movement_id=str(movement), carrier_id=str(_get(payload, m["carrier_field"])),
                       bol_number=str(_get(payload, m["bol_field"])), container_or_job_id=str(_get(payload, m["ref_field"])),
                       location=_get(payload, m.get("location_field")), events=events)
    return batch, skipped


# ----------------------------- outbound ----------------------------- #
def enqueue(session: Session, kind: str, movement_id: str, payload: Dict[str, Any]) -> int:
    n = 0
    for t in session.query(IntegrationTarget).filter_by(enabled=True).all():
        if kind in (t.subscribes or []):
            session.add(OutboxMessage(target=t.name, kind=kind, movement_id=movement_id, payload=payload))
            n += 1
    return n


def case_payload(case: Case, lines: Optional[List[dict]] = None) -> Dict[str, Any]:
    return {"movement_id": case.movement_id, "charge_code": case.charge_code, "status": case.status,
            "claimed": case.claimed, "supported": case.supported, "agreed_amount": case.agreed_amount,
            "paid_amount": case.paid_amount, "paid_reference": case.paid_reference, "reason_code": case.reason_code,
            "decision_owner": case.decision_owner, "next_action": case.next_action, "location": case.location,
            "lines": lines or []}


def emit_case_outcome(session: Session, case: Case, lines: Optional[List[dict]] = None) -> int:
    """Queue the outcome that this case status maps to (finance posting, dispute pack, correction request...)."""
    row = session.get(StatusOutcome, case.status)
    return enqueue(session, row.kind, case.movement_id, case_payload(case, lines)) if row else 0


def dispatch(session: Session, cfg: Optional[Settings] = None, client: Optional[httpx.Client] = None) -> Dict[str, int]:
    """Deliver pending messages once; failures are retried on later runs until `outbox_max_attempts`."""
    cfg = cfg or get_settings()
    targets = {t.name: t for t in session.query(IntegrationTarget).all()}
    sent = failed = 0
    own = client is None
    client = client or httpx.Client(timeout=cfg.outbox_timeout_s)
    try:
        for msg in session.query(OutboxMessage).filter_by(status="PENDING").order_by(OutboxMessage.id).all():
            t = targets.get(msg.target)
            if t is None or not t.enabled:
                continue
            headers = {"Content-Type": "application/json", "Idempotency-Key": f"outbox-{msg.id}"}
            token = os.environ.get(t.auth_env or "", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            msg.attempts += 1
            try:
                r = client.post(t.url, json={"kind": msg.kind, "movement_id": msg.movement_id, "data": msg.payload},
                                headers=headers)
                r.raise_for_status()
                msg.status, msg.sent_at, msg.last_error = "SENT", datetime.now(timezone.utc), None
                sent += 1
            except Exception as exc:  # network or HTTP error: keep for retry
                msg.last_error = str(exc)[:500]
                if msg.attempts >= cfg.outbox_max_attempts:
                    msg.status = "FAILED"
                    failed += 1
                log.warning("outbox_delivery_failed", extra={"id": msg.id, "target": msg.target, "error": msg.last_error})
        session.commit()
    finally:
        if own:
            client.close()
    return {"sent": sent, "failed": failed}


if __name__ == "__main__":
    from sqlalchemy.orm import sessionmaker

    from .db import get_engine
    with sessionmaker(get_engine())() as s:
        print(dispatch(s))
