"""System-administrator operations: contract versions, charge catalog, source ranks, permissions, calendar.

Every change is written to the audit log (who, old value, new value, reason). No code deploy is needed
to change a rule or a rate.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import (
    AuditLog,
    ChargeCatalog,
    Contract,
    Document,
    NonWorkingDay,
    RolePermission,
    SourceReliability,
)
from .security import Principal


def _audit(s: Session, entity: str, entity_id: str, field: str, old, new, reason: str, who: Principal) -> None:
    dump = lambda v: None if v is None else json.dumps(v, default=str, sort_keys=True)  # noqa: E731
    s.add(AuditLog(entity=entity, entity_id=entity_id, field=field, old_value=dump(old), new_value=dump(new),
                   reason=reason, actor=who.user, role=who.role))


class ContractIn(BaseModel):
    carrier_id: str
    charge_code: str
    effective_from: date
    terms: dict
    clause_ref: Optional[str] = None
    clause_text: Optional[str] = None
    reason: str = Field(min_length=3)


def new_contract_version(s: Session, c: ContractIn, who: Principal) -> Contract:
    """Add a version effective from a date; the previous open version ends the day before."""
    if s.get(ChargeCatalog, c.charge_code) is None:
        raise ValueError(f"Unknown charge code {c.charge_code}")
    if s.query(Contract).filter_by(carrier_id=c.carrier_id, charge_code=c.charge_code, effective_from=c.effective_from).first():
        raise ValueError("A version already starts on that date")
    prev = (s.query(Contract).filter(Contract.carrier_id == c.carrier_id, Contract.charge_code == c.charge_code,
                                     Contract.effective_from < c.effective_from)
            .order_by(Contract.effective_from.desc()).first())
    if prev and (prev.effective_to is None or prev.effective_to >= c.effective_from):
        _audit(s, "contract", f"{c.carrier_id}:{c.charge_code}", "effective_to", prev.effective_to,
               c.effective_from - timedelta(days=1), c.reason, who)
        prev.effective_to = c.effective_from - timedelta(days=1)
    row = Contract(carrier_id=c.carrier_id, charge_code=c.charge_code, effective_from=c.effective_from, terms=c.terms,
                   clause_ref=c.clause_ref, clause_text=c.clause_text)
    s.add(row)
    _audit(s, "contract", f"{c.carrier_id}:{c.charge_code}", "version", prev.terms if prev else None, c.terms, c.reason, who)
    s.commit()
    return row


def set_role_permissions(s: Session, role: str, permissions: List[str], reason: str, who: Principal) -> None:
    old = sorted(r.permission for r in s.query(RolePermission).filter_by(role=role).all())
    s.query(RolePermission).filter_by(role=role).delete()
    for p in sorted(set(permissions)):
        s.add(RolePermission(role=role, permission=p))
    _audit(s, "role", role, "permissions", old, sorted(set(permissions)), reason, who)
    s.commit()


def set_source_rank(s: Session, event_type: str, source: str, rank: int, reason: str, who: Principal) -> None:
    row = s.get(SourceReliability, (event_type, source))
    _audit(s, "source_reliability", f"{event_type}:{source}", "rank", row.rank if row else None, rank, reason, who)
    if row:
        row.rank = rank
    else:
        s.add(SourceReliability(event_type=event_type, source=source, rank=rank))
    s.commit()


def add_non_working_days(s: Session, location: str, days: Dict[date, str], reason: str, who: Principal) -> int:
    n = 0
    for d, name in days.items():
        if s.get(NonWorkingDay, (location, d)) is None:
            s.add(NonWorkingDay(location=location, day=d, name=name))
            n += 1
    _audit(s, "calendar", location, "days_added", None, [str(d) for d in days], reason, who)
    s.commit()
    return n


def upsert_charge(s: Session, code: str, description: str, requires_authorization: bool, reason: str, who: Principal) -> None:
    row = s.get(ChargeCatalog, code)
    _audit(s, "charge_catalog", code, "definition", None if row is None else
           {"description": row.description, "requires_authorization": row.requires_authorization},
           {"description": description, "requires_authorization": requires_authorization}, reason, who)
    if row:
        row.description, row.requires_authorization = description, requires_authorization
    else:
        s.add(ChargeCatalog(code=code, description=description, requires_authorization=requires_authorization))
    s.commit()


# ----------------------------- documents ----------------------------- #
def save_document(s: Session, movement_id: str, kind: str, filename: str, data: bytes, who: Principal,
                  cfg: Optional[Settings] = None) -> Document:
    cfg = cfg or get_settings()
    if len(data) > cfg.max_document_bytes:
        raise ValueError(f"Document exceeds {cfg.max_document_bytes} bytes")
    digest = hashlib.sha256(data).hexdigest()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name) or "document"
    folder = cfg.document_dir / re.sub(r"[^A-Za-z0-9._-]", "_", movement_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest[:12]}_{safe}"
    path.write_bytes(data)
    doc = Document(movement_id=movement_id, kind=kind, filename=safe, storage_path=str(path), sha256=digest,
                   size_bytes=len(data), uploaded_by=who.user)
    s.add(doc)
    s.commit()
    return doc
