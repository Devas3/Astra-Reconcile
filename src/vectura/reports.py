"""Manager-facing reports: repeated causes of charges by facility / terminal."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import ActionPolicy, Case


def repeat_causes(s: Session, location: Optional[str] = None, cfg: Optional[Settings] = None) -> List[dict]:
    """Group cases that carry a cause (reason code other than NONE) by facility, so recurring problems stand out."""
    cfg = cfg or get_settings()
    policies = {p.reason_code: p for p in s.query(ActionPolicy).all()}
    q = s.query(Case).filter(Case.reason_code != "NONE")
    if location:
        q = q.filter(Case.location == location)
    groups: Dict[Tuple[str, str, str], dict] = defaultdict(lambda: {"cases": 0, "claimed": 0.0, "supported": 0.0, "movements": []})
    for c in q.all():
        g = groups[(c.location or "(unknown)", c.reason_code, c.charge_code)]
        g["cases"] += 1
        g["claimed"] += c.claimed
        g["supported"] += c.supported
        g["movements"].append(c.movement_id)
    out = []
    for (loc, reason, code), g in groups.items():
        out.append({"location": loc, "reason_code": reason, "charge_code": code, "cases": g["cases"],
                    "disputed_value": round(g["claimed"] - g["supported"], 2), "movements": g["movements"][:20],
                    "repeated": g["cases"] >= cfg.repeat_cause_threshold,
                    "decision_owner": policies[reason].decision_owner if reason in policies else ""})
    return sorted(out, key=lambda r: (not r["repeated"], -r["cases"], -r["disputed_value"]))
