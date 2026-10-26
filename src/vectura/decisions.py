"""Case decisions with role permissions and a full manual-change audit trail."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .integrations import emit_case_outcome
from .models import AuditLog, AuditRecord, Case, DecisionType
from .schemas import DecisionRequest
from .security import Principal, has_permission


def _log(session: Session, case: Case, field: str, old, new, reason: str, who: Principal) -> None:
    session.add(AuditLog(entity="case", entity_id=f"{case.movement_id}:{case.charge_code}", field=field,
                         old_value=None if old is None else str(old), new_value=None if new is None else str(new),
                         reason=reason, actor=who.user, role=who.role))


def decide(session: Session, req: DecisionRequest, who: Principal, cfg: Optional[Settings] = None) -> Case:
    cfg = cfg or get_settings()
    dt = session.get(DecisionType, req.decision)
    if dt is None:
        raise ValueError(f"Unknown decision '{req.decision}'")
    if not has_permission(session, who.role, dt.permission):
        raise PermissionError(f"Role '{who.role}' lacks permission '{dt.permission}' for {dt.code}")
    case = session.query(Case).filter_by(movement_id=req.movement_id, charge_code=req.charge_code).first()
    if case is None:
        raise LookupError("No case for that movement and charge")
    if dt.requires_reason and not (req.reason and req.reason.strip()):
        raise ValueError(f"{dt.code} requires a reason")
    if dt.allowed_from and case.status not in dt.allowed_from:
        raise ValueError(f"{dt.code} is not allowed while the case is {case.status}")
    reason = (req.reason or dt.code).strip()

    if dt.code == "OVERRIDE_AMOUNT":
        if req.amount is None or req.amount > case.claimed:
            raise ValueError("OVERRIDE_AMOUNT needs an amount not above the claimed value")
        _log(session, case, "supported", case.supported, req.amount, reason, who)
        case.supported = req.amount
    elif dt.code == "WRITE_OFF":
        open_value = round(case.claimed - case.supported, 2)
        amt = req.amount if req.amount is not None else open_value
        if amt <= 0 or amt > open_value + cfg.money_tolerance:
            raise ValueError(f"Write-off must be between 0 and the open disputed value (${open_value:.2f})")
        if amt > cfg.significant_writeoff_amount and not has_permission(session, who.role, "approve_large_writeoff"):
            raise PermissionError(f"Write-offs above ${cfg.significant_writeoff_amount:.2f} need a manager")
        _log(session, case, "written_off_amount", None, amt, reason, who)
    elif dt.code == "AGREE_SETTLEMENT":
        if req.amount is None or req.amount > case.claimed + cfg.money_tolerance:
            raise ValueError("AGREE_SETTLEMENT needs the agreed amount, not above the claimed value")
        _log(session, case, "agreed_amount", case.agreed_amount, req.amount, reason, who)
        case.agreed_amount = req.amount
        _log(session, case, "supported", case.supported, req.amount, "carrier agreement: " + reason, who)
        case.supported = req.amount
    elif dt.code == "MARK_PAID":
        due = case.agreed_amount if case.agreed_amount is not None else case.supported
        paid = req.amount if req.amount is not None else due
        if abs(paid - due) > cfg.money_tolerance:
            raise ValueError(f"Paid amount ${paid:.2f} differs from the agreed/supported amount ${due:.2f}; "
                             "record an agreement or override first")
        _log(session, case, "paid_amount", case.paid_amount, paid, reason, who)
        case.paid_amount, case.paid_reference = paid, req.reference
        rec = session.get(AuditRecord, case.claim_hash)
        if rec:
            _log(session, case, "audit_record_status", rec.status, "PAID", reason, who)
            rec.status = "PAID"
    _log(session, case, "status", case.status, dt.resulting_status, reason, who)
    case.status = dt.resulting_status
    session.flush()
    emit_case_outcome(session, case)
    session.commit()
    return case
