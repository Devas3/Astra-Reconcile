"""Deterministic anti-fraud and audit guardrails (Module 2). No LLMs; all rules come from the DB."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Iterable, List, Optional, Set, Tuple

from pydantic import BaseModel
from sqlalchemy.orm import Session

from .charge_clock_engine import money
from .models import AuditRecord, ConflictRule

BLOCKING_STATUSES = ("APPROVED", "PAID")


class FraudAlert(BaseModel):
    alert_type: str  # DUPLICATE_CLAIM | UNAUTHORIZED_GHOST_FEE | OVERLAPPING_CLOCK | FUEL_PEG_VIOLATION
    charge_code: str = ""
    detail: str = ""


class TimeClaim(BaseModel):
    charge_code: str
    start: datetime
    end: datetime


def claim_hash(carrier_id: str, bol_number: str, charge_code: str, container_or_job_id: str) -> str:
    """Composite SHA-256 over the claim identity."""
    raw = "|".join(s.strip().upper() for s in (carrier_id, bol_number, charge_code, container_or_job_id))
    return hashlib.sha256(raw.encode()).hexdigest()


def check_duplicate(session: Session, h: str, code: str, bol: str, ref: str) -> Optional[FraudAlert]:
    """DUPLICATE_CLAIM if an APPROVED/PAID record with this hash already exists."""
    rec = session.get(AuditRecord, h)
    if rec and rec.status in BLOCKING_STATUSES:
        return FraudAlert(
            alert_type="DUPLICATE_CLAIM", charge_code=code,
            detail=f"{code} for BOL {bol} / {ref} already {rec.status.lower()} (movement {rec.movement_id}); "
                   "$0 authorized, 100% disputed.")
    return None


def check_ghost_fee(code: str, requires_authorization: bool, on_contract: bool, has_receipt: bool) -> Optional[FraudAlert]:
    """UNAUTHORIZED_GHOST_FEE: catalog says authorization is required, no Rate-Con row, no verified receipt."""
    if requires_authorization and not on_contract and not has_receipt:
        return FraudAlert(
            alert_type="UNAUTHORIZED_GHOST_FEE", charge_code=code,
            detail=f"{code} is not pre-authorized on the Rate-Con / Work Order and has no verified receipt.")
    return None


def load_conflict_pairs(session: Session) -> Set[frozenset]:
    return {frozenset({r.code_a, r.code_b}) for r in session.query(ConflictRule).all()}


def check_overlapping_clocks(claims: List[TimeClaim], conflict_pairs: Iterable[frozenset]) -> List[Tuple[TimeClaim, TimeClaim]]:
    """Pairs of claims whose codes conflict and whose time windows overlap."""
    pairs = set(conflict_pairs)
    out = []
    for i, a in enumerate(claims):
        for b in claims[i + 1:]:
            if frozenset({a.charge_code, b.charge_code}) in pairs and a.start < b.end and b.start < a.end:
                out.append((a, b))
    return out


def check_fuel_peg(expected: float, claimed: float, tolerance: float, context: str) -> Tuple[float, Optional[FraudAlert]]:
    """Return (supported, alert) enforcing the DOE-pegged fuel amount."""
    supported = money(expected)
    if abs(supported - claimed) > tolerance:
        return supported, FraudAlert(
            alert_type="FUEL_PEG_VIOLATION", charge_code="FSC",
            detail=f"Claimed ${claimed:.2f}; DOE peg gives ${supported:.2f} ({context}).")
    return supported, None
