"""Idempotent loader for reference data in data/seed/*.json. Run: python -m vectura.seed"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .db import get_engine
from .models import (
    ActionPolicy,
    ChargeCatalog,
    ConflictRule,
    Contract,
    DecisionType,
    EventType,
    FuelIndexWeek,
    IntegrationSource,
    IntegrationTarget,
    NonWorkingDay,
    RolePermission,
    Scenario,
    SourceReliability,
    StatusOutcome,
)

log = logging.getLogger(__name__)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def seed(session: Session, seed_dir: Optional[Path] = None, *, scenarios: bool = True) -> None:
    d = seed_dir or get_settings().seed_dir
    cat = _load(d / "catalog.json")
    for c in cat["charges"]:
        session.merge(ChargeCatalog(**c))
    session.flush()
    for a, b in cat["conflicts"]:
        if not session.query(ConflictRule).filter_by(code_a=a, code_b=b).first():
            session.add(ConflictRule(code_a=a, code_b=b))
    for c in _load(d / "contracts.json")["contracts"]:
        eff = date.fromisoformat(c["effective_from"])
        row = session.query(Contract).filter_by(
            carrier_id=c["carrier_id"], charge_code=c["charge_code"], effective_from=eff).first()
        if row:
            row.terms = c["terms"]
            row.clause_ref, row.clause_text = c.get("clause_ref"), c.get("clause_text")
        else:
            session.add(Contract(carrier_id=c["carrier_id"], charge_code=c["charge_code"],
                                 terms=c["terms"], effective_from=eff,
                                 clause_ref=c.get("clause_ref"), clause_text=c.get("clause_text")))
    for w in _load(d / "fuel_index.json")["weeks"]:  # never overwrite live EIA rows
        wk = date.fromisoformat(w["week_start"])
        if not session.get(FuelIndexWeek, wk):
            session.add(FuelIndexWeek(week_start=wk, price=w["price"], source="SEED"))
    ev = _load(d / "events.json")
    for e in ev["event_types"]:
        session.merge(EventType(**e))
    session.flush()
    for r in ev["source_reliability"]:
        session.merge(SourceReliability(**r))
    pol = _load(d / "policy.json")
    for a in pol["action_policies"]:
        session.merge(ActionPolicy(**a))
    for role, perms in pol["role_permissions"].items():
        for perm in perms:
            session.merge(RolePermission(role=role, permission=perm))
    for o in pol["status_outcomes"]:
        session.merge(StatusOutcome(**o))
    for t in pol["decision_types"]:
        session.merge(DecisionType(**t))
    for n in _load(d / "calendar.json")["non_working_days"]:
        session.merge(NonWorkingDay(location=n["location"], day=date.fromisoformat(n["day"]), name=n["name"]))
    integ = _load(d / "integrations.json")
    for src in integ["sources"]:
        session.merge(IntegrationSource(**src))
    for t in integ["targets"]:
        session.merge(IntegrationTarget(**t))
    if scenarios:
        for s in _load(d / "scenarios.json")["scenarios"]:
            session.merge(Scenario(name=s["name"], label=s["label"], description=s["description"], mode=s["mode"],
                                   repeat=s["repeat"], sort_order=s["sort_order"], request=s["request"]))
    session.commit()
    log.info("seed_complete")


if __name__ == "__main__":
    with sessionmaker(get_engine())() as s:
        seed(s)
    print("seeded")
