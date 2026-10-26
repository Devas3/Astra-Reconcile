"""Reconcile service: contracts + evidence + fraud guardrails -> 2-stage settlement. All rules come from the DB."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .charge_clock_engine import (  # re-exported: the 4-field schema lives alongside the clocks
    ClockResult,
    ClockStatus,
    DemurrageInput,
    DetentionInput,
    IntermodalInput,
    LineItemReconciliation,
    RailInput,
    ReconciliationAction,
    ReconciliationResult,
    ServiceInput,
    StorageInput,
    TLDetentionInput,
    evaluate_intermodal_storage,
    evaluate_ocean_demurrage,
    evaluate_ocean_detention,
    evaluate_rail_demurrage,
    evaluate_service,
    evaluate_storage,
    evaluate_tl_detention,
    money,
)
from .config import Settings, get_settings
from .eia_fuel_index import diesel_price, fsc_per_mile, prevailing_monday
from .events import recorded_evidence
from .fraud_guardrails import (
    FraudAlert,
    check_duplicate,
    check_fuel_peg,
    check_ghost_fee,
    check_overlapping_clocks,
    claim_hash,
    load_conflict_pairs,
)
from .integrations import emit_case_outcome
from .models import (
    ActionPolicy,
    AuditRecord,
    Case,
    ChargeCatalog,
    Contract,
    EventType,
    FraudAlertRow,
    InvoiceRecord,
    NonWorkingDay,
    Settlement,
)
from .schemas import ActualRequest, Evidence, ProvisionalRequest, ServiceEvidence, TLEvidence

__all__ = ["ReconciliationAction", "LineItemReconciliation", "ReconciliationResult", "Reconciler"]
log = logging.getLogger(__name__)
A = ReconciliationAction
GROUP_CHARGE = {"tl": "DET", "service": "SVC"}  # which charge an event group prices


class Reconciler:
    def __init__(self, session: Session, settings: Optional[Settings] = None):
        self.s = session
        self.cfg = settings or get_settings()
        self.catalog: Dict[str, ChargeCatalog] = {c.code: c for c in session.query(ChargeCatalog).all()}
        self.policies: Dict[str, ActionPolicy] = {p.reason_code: p for p in session.query(ActionPolicy).all()}
        self.event_types = session.query(EventType).all()

    # ----------------------------- lookups ----------------------------- #
    def terms(self, carrier: str, code: str, on: date) -> Optional[dict]:
        row = (self.s.query(Contract)
               .filter(Contract.carrier_id == carrier, Contract.charge_code == code,
                       Contract.effective_from <= on,
                       or_(Contract.effective_to.is_(None), Contract.effective_to >= on))
               .order_by(Contract.effective_from.desc()).first())
        return row.terms if row else None

    def _clause(self, carrier: str, code: str, ev: Evidence) -> Optional[str]:
        for d in self._evidence_dates(ev) or [date.today()]:
            row = (self.s.query(Contract)
                   .filter(Contract.carrier_id == carrier, Contract.charge_code == code, Contract.effective_from <= d,
                           or_(Contract.effective_to.is_(None), Contract.effective_to >= d))
                   .order_by(Contract.effective_from.desc()).first())
            if row and row.clause_ref:
                return f"{row.clause_ref}: {row.clause_text}" if row.clause_text else row.clause_ref
        return None

    def _terms_for(self, carrier: str, code: str, ev: Evidence) -> Optional[dict]:
        """Terms in force on the movement's own dates (never today's terms for an old charge)."""
        for d in self._evidence_dates(ev) or [date.today()]:
            t = self.terms(carrier, code, d)
            if t is not None:
                return t
        return None

    def _line(self, code, claimed, supported, action, findings, reason="NONE", refs=None, desc=None,
              facts=None, expected=None) -> LineItemReconciliation:
        supported = money(min(max(supported, 0.0), claimed))
        cat = self.catalog.get(code)
        pol = self.policies[reason]
        return LineItemReconciliation(
            charge_code=code, description=desc or (cat.description if cat else code),
            amount_claimed=money(claimed), amount_supported=supported, variance=money(claimed - supported),
            recommended_action=action, findings=findings, proof_references=refs or [], reason_code=reason,
            decision_owner=pol.decision_owner, next_action=pol.next_action, evidence_facts=facts or [],
            provisional_amount=expected)

    def _by_amount(self, claimed: float, supported: float) -> Tuple[ReconciliationAction, str]:
        if abs(claimed - supported) <= self.cfg.money_tolerance:
            return A.APPROVE, "NONE"
        return A.PARTIAL_APPROVE, "OVER_CLAIM"

    @staticmethod
    def _overall(lines, alerts) -> ReconciliationAction:
        acts = {li.recommended_action for li in lines}
        if alerts or A.HITL_REVIEW in acts:
            return A.HITL_REVIEW
        if A.REQUEST_MISSING_PROOF in acts:
            return A.REQUEST_MISSING_PROOF
        if A.PARTIAL_APPROVE in acts:
            return A.PARTIAL_APPROVE
        return A.APPROVE

    def _expected(self, movement_id: str) -> Dict[str, float]:
        """Pre-bill expected amount per charge: the provisional invoice, overridden by an event-driven proposal."""
        out: Dict[str, float] = {}
        for stage in ("PROVISIONAL", "PROPOSED"):
            row = (self.s.query(Settlement).filter_by(movement_id=movement_id, stage=stage)
                   .order_by(Settlement.id.desc()).first())
            if row:
                for li in row.payload["line_items"]:
                    out[li["charge_code"]] = li["amount_supported"]
        return out

    def _persist(self, movement_id, stage, lines, alerts, expected=None) -> ReconciliationResult:
        res = ReconciliationResult(
            movement_id=movement_id, stage=stage,
            total_claimed=money(sum(i.amount_claimed for i in lines)),
            total_supported=money(sum(i.amount_supported for i in lines)),
            total_variance=money(sum(i.variance for i in lines)),
            overall_action=self._overall(lines, alerts),
            fraud_alerts=[f"{a.alert_type}: {a.detail}" for a in alerts], line_items=lines)
        if expected:
            res.provisional_total = money(sum(expected.values()))
            res.variance_vs_provisional = money(res.total_supported - res.provisional_total)
        seq = self.s.query(func.count(Settlement.id)).filter_by(movement_id=movement_id, stage=stage).scalar() or 0
        digest = hashlib.sha256(
            (json.dumps(res.model_dump(mode="json"), sort_keys=True) + f"#{seq}").encode()).hexdigest()
        res.audit_proof_pack_id = f"APP-{digest[:16].upper()}"
        self.s.add(Settlement(movement_id=movement_id, stage=stage, proof_id=res.audit_proof_pack_id,
                              payload=res.model_dump(mode="json")))
        for a in alerts:
            self.s.add(FraudAlertRow(movement_id=movement_id, alert_type=a.alert_type,
                                     charge_code=a.charge_code, detail=a.detail))
        return res

    # ----------------------------- stage 1: provisional ----------------------------- #
    def provisional(self, req: ProvisionalRequest) -> ReconciliationResult:
        on = req.origin_departure.date()
        lh = self.terms(req.carrier_id, "LH", on)
        if lh is None:
            raise LookupError(f"No linehaul contract for carrier {req.carrier_id} on {on}")
        linehaul = money(req.miles * lh["rate_per_mile"])
        lines = [self._line("LH", linehaul, linehaul, A.APPROVE,
                            f"Pro-forma linehaul {req.origin} -> {req.destination}, "
                            f"departed {req.origin_departure.isoformat()}.")]
        fsc_terms, price = self.terms(req.carrier_id, "FSC", on), diesel_price(self.s, on)
        if fsc_terms and price is not None:
            rate = fsc_per_mile(price, fsc_terms["base_price"], fsc_terms["mpg"], self.cfg.fsc_rate_decimals)
            fsc = money(rate * req.miles)
            lines.append(self._line("FSC", fsc, fsc, A.APPROVE,
                                    f"DOE index week of {prevailing_monday(on)}: ${rate}/mi x {req.miles} mi."))
        res = self._persist(req.movement_id, "PROVISIONAL", lines, [])
        self.s.commit()
        return res

    # ----------------------------- event-driven proposal ----------------------------- #
    def propose(self, movement_id: str, carrier_id: str, ref: str) -> Optional[ReconciliationResult]:
        """When a clock-closing event arrives, price what the recorded events support (pre-bill, preventive)."""
        rec = recorded_evidence(self.s, movement_id, self.cfg)
        ev = Evidence(
            tl=TLEvidence(**rec.groups["tl"]) if "tl" in rec.groups else None,
            service=ServiceEvidence(job_id=ref, **rec.groups["service"]) if "service" in rec.groups else None)
        lines = []
        for group, code in GROUP_CHARGE.items():
            if group not in rec.groups:
                continue
            clk, err = self._clock(code, carrier_id, ev)
            if err or clk is None or clk.status != ClockStatus.OK:
                continue
            lines.append(self._line(code, clk.amount_supported, clk.amount_supported, A.APPROVE,
                                    "System-generated from verified events: " + " ".join(clk.notes),
                                    refs=clk.proof_references, facts=rec.facts.get(group)))
        return self._persist(movement_id, "PROPOSED", lines, []) if lines else None

    # ----------------------------- evidence helpers ----------------------------- #
    @staticmethod
    def _evidence_dates(ev: Evidence) -> List[date]:
        out: List[date] = []
        if ev.tl:
            out += [t.date() for t in (ev.tl.release_time, ev.tl.arrival_time, ev.tl.appointment_time) if t]
        if ev.demurrage:
            out.append(ev.demurrage.gate_out_date or ev.demurrage.last_free_day)
        if ev.detention:
            out.append(ev.detention.empty_return_date or ev.detention.gate_out_date)
        if ev.storage:
            out.append(ev.storage.pickup_date or ev.storage.storage_start_date)
        if ev.rail:
            out.append(ev.rail.release_date or ev.rail.placement_date)
        if ev.intermodal and (ev.intermodal.pickup_date or ev.intermodal.ramp_available_date):
            out.append(ev.intermodal.pickup_date or ev.intermodal.ramp_available_date)
        if ev.service:
            out += [t.date() for t in (ev.service.customer_otp_sign_off, ev.service.technician_check_in) if t]
        if ev.fuel:
            out.append(ev.fuel.ship_date)
        return out

    def _merged_evidence(self, req: ActualRequest):
        """Inline evidence first; recorded events fill the gaps. Returns (evidence, facts by group, conflicts)."""
        ev = req.evidence
        facts: Dict[str, List[str]] = {}
        conflicts: List[str] = []
        rec = recorded_evidence(self.s, req.movement_id, self.cfg) if req.use_recorded_events else None
        self._meta = rec.meta if rec else None
        ev.location = ev.location or (self._meta.location if self._meta else None)
        self._merge_calendar(ev)
        tz = ev.timezone or self.cfg.default_timezone
        by_group: Dict[str, List[EventType]] = {}
        for t in self.event_types:
            by_group.setdefault(t.evidence_group, []).append(t)
        for group, cls in {"tl": TLEvidence, "service": ServiceEvidence}.items():
            inline = getattr(ev, group)
            fields = dict(rec.groups.get(group, {})) if rec else {}
            if inline is None and fields:
                extra = {"job_id": req.container_or_job_id} if group == "service" else {}
                inline = cls(**extra, **fields)
                setattr(ev, group, inline)
            elif inline is not None:
                for k, v in fields.items():
                    if getattr(inline, k) is None:
                        setattr(inline, k, v)
            if inline is None:
                continue
            f = list(rec.facts.get(group, [])) if rec else []
            recorded_fields = set(rec.groups.get(group, {})) if rec else set()
            for t in by_group.get(group, []):
                val = getattr(inline, t.evidence_field, None)
                if val is None:
                    f.append(f"{t.description} · NOT RECORDED")
                elif t.evidence_field not in recorded_fields:
                    f.append(f"{t.description} · submitted · {val.astimezone(ZoneInfo(tz)):%Y-%m-%d %H:%M %Z} ({val:%H:%M}Z)")
            facts[group] = f
        if rec:
            conflicts += rec.conflicts
        if ev.tl and ev.tl.arrival_time and ev.tl.app_arrival_time:
            gap = abs((ev.tl.arrival_time - ev.tl.app_arrival_time).total_seconds()) / 60
            if gap > self.cfg.conflict_tolerance_minutes:
                conflicts.append(f"Arrival sources disagree by {gap:.0f} min (GPS/gate vs driver app); "
                                 "neither was substituted for the missing or contractual record.")
        return ev, facts, conflicts

    def _merge_calendar(self, ev: Evidence) -> None:
        """Add the facility/terminal's non-working days to every date-based clock (never removes submitted ones)."""
        if not ev.location:
            return
        days = {r.day for r in self.s.query(NonWorkingDay).filter_by(location=ev.location).all()}
        if ev.demurrage:
            ev.demurrage.terminal_closed_dates = sorted(set(ev.demurrage.terminal_closed_dates) | days)
        for grp in (ev.storage, ev.rail, ev.intermodal):
            if grp is not None:
                grp.closed_dates = sorted(set(grp.closed_dates) | days)

    def _det_exceptions(self, carrier: str, ev: Evidence):
        """Split claimed exceptions into minutes the contract recognises (with proof) and ones needing a finding."""
        t = self._terms_for(carrier, "DET", ev) or {}
        known, excluded, unresolved = t.get("exceptions", {}), 0, []
        for e in ev.exceptions:
            c = known.get(e.code)
            if c and c.get("effect") == "EXCLUDE_MINUTES" and c.get("proof") in ev.proofs and e.minutes:
                excluded += e.minutes
            else:
                why = "not in the contract" if not c else ("proof not attached" if c.get("effect") == "EXCLUDE_MINUTES" else "responsibility undetermined")
                unresolved.append((e.code, e.minutes, (c or {}).get("responsibility", "UNDETERMINED"), why))
        return excluded, unresolved

    @staticmethod
    def _hold_dates(d) -> dict:
        out = {"verified_hold_dates": [], "unverified_hold_dates": []}
        for h in d.holds:
            n = (h.end_date - h.start_date).days
            days = [h.start_date + timedelta(days=i) for i in range(n + 1)]
            out["verified_hold_dates" if h.verified else "unverified_hold_dates"].extend(days)
        return out

    def _clock(self, code: str, carrier: str, ev: Evidence) -> Tuple[Optional[ClockResult], Optional[str]]:
        """(clock result, error). error = no contract terms apply; (None, None) = not a clock-based charge."""
        def build(fn, model, evidence, ref_date, extra=None):
            t = self.terms(carrier, code, ref_date)
            if t is None:
                return None, f"No {code} terms in the carrier's contract for {ref_date}: contract review needed."
            return fn(model(**{**evidence.model_dump(), **(extra or {}), **t})), None

        now = datetime.now(timezone.utc)
        if code == "DET" and ev.tl:
            return build(evaluate_tl_detention, TLDetentionInput, ev.tl,
                         (ev.tl.release_time or ev.tl.arrival_time or now).date(),
                         {"excluded_minutes": self._det_exceptions(carrier, ev)[0]})
        if code == "DEM" and ev.demurrage:
            return build(evaluate_ocean_demurrage, DemurrageInput, ev.demurrage,
                         ev.demurrage.gate_out_date or ev.demurrage.last_free_day, self._hold_dates(ev.demurrage))
        if code in ("OCN_DET", "IM_DET") and ev.detention:
            return build(evaluate_ocean_detention, DetentionInput, ev.detention,
                         ev.detention.empty_return_date or ev.detention.gate_out_date)
        if code in ("STOR", "LTL_STOR") and ev.storage:
            return build(evaluate_storage, StorageInput, ev.storage,
                         ev.storage.pickup_date or ev.storage.storage_start_date)
        if code == "IM_STOR" and ev.intermodal:
            return build(evaluate_intermodal_storage, IntermodalInput, ev.intermodal,
                         ev.intermodal.pickup_date or ev.intermodal.ramp_available_date or now.date())
        if code == "RAIL_DEM" and ev.rail:
            return build(evaluate_rail_demurrage, RailInput, ev.rail, ev.rail.release_date or ev.rail.placement_date)
        if code == "SVC" and ev.service:
            return build(evaluate_service, ServiceInput, ev.service,
                         (ev.service.customer_otp_sign_off or ev.service.technician_check_in or now).date())
        return None, None

    # ----------------------------- stage 2: actual ----------------------------- #
    def actual(self, req: ActualRequest) -> ReconciliationResult:
        if req.dry_run:  # what-if: run every rule, then discard all writes (cases, audit records, outbox)
            try:
                return self._actual(req)
            finally:
                self.s.rollback()
        try:
            res = self._actual(req)
            self.s.commit()
            return res
        except IntegrityError:  # a concurrent duplicate won the race: redo, the guard will now see it
            self.s.rollback()
            res = self._actual(req)
            self.s.commit()
            return res

    def _actual(self, req: ActualRequest) -> ReconciliationResult:
        alerts: List[FraudAlert] = []
        lines: List[LineItemReconciliation] = []
        per_charge: Dict[str, List[LineItemReconciliation]] = {}
        hashes: Dict[str, str] = {}
        ev, facts, conflicts = self._merged_evidence(req)
        expected = self._expected(req.movement_id)
        overlap_codes = set()
        blocked: Optional[Tuple[str, FraudAlert]] = None
        meta = self._meta
        if meta is not None:
            diffs = [f"{n}: bill has '{a}', events on record have '{b}'" for n, a, b in (
                ("carrier", req.carrier_id, meta.carrier_id), ("BOL", req.bol_number, meta.bol_number),
                ("container/job", req.container_or_job_id, meta.container_or_job_id)) if a != b]
            if diffs:
                blocked = ("REFERENCE_MISMATCH", FraudAlert(alert_type="REFERENCE_MISMATCH", charge_code="*",
                                                            detail="; ".join(diffs)))
        if blocked is None and req.invoice_number and self.s.query(InvoiceRecord).filter_by(
                carrier_id=req.carrier_id, invoice_number=req.invoice_number).first():
            blocked = ("DUPLICATE_INVOICE", FraudAlert(alert_type="DUPLICATE_INVOICE", charge_code="*",
                                                       detail=f"Invoice number {req.invoice_number} was already processed."))
        if blocked:
            alerts.append(blocked[1])
        for a, b in check_overlapping_clocks(ev.time_claims, load_conflict_pairs(self.s)):
            alerts.append(FraudAlert(alert_type="OVERLAPPING_CLOCK", charge_code=f"{a.charge_code}/{b.charge_code}",
                                     detail=f"{a.charge_code} and {b.charge_code} claim the same time window."))
            overlap_codes.update({a.charge_code, b.charge_code})

        for ch in req.claimed_charges:
            code, claimed, desc = ch.charge_code, ch.amount, ch.description
            exp = expected.get(code)
            h = hashes[code] = claim_hash(req.carrier_id, req.bol_number, code, req.container_or_job_id)
            cat = self.catalog.get(code)
            if blocked:
                out = [self._line(code, claimed, 0, A.HITL_REVIEW, f"{blocked[0]}: {blocked[1].detail}", blocked[0],
                                  desc=desc, expected=exp)]
            elif cat is None:
                out = [self._line(code, claimed, 0, A.HITL_REVIEW, "Unknown charge code (not in catalog).",
                                  "UNKNOWN_CHARGE", desc=desc, expected=exp)]
            elif (dup := check_duplicate(self.s, h, code, req.bol_number, req.container_or_job_id)):
                alerts.append(dup)
                out = [self._line(code, claimed, 0, A.HITL_REVIEW, "DUPLICATE_CLAIM: " + dup.detail, "DUPLICATE",
                                  desc=desc, expected=exp)]
            elif (ghost := check_ghost_fee(code, cat.requires_authorization,
                                           self._terms_for(req.carrier_id, code, ev) is not None,
                                           bool(ch.receipt_ref))):
                alerts.append(ghost)
                out = [self._line(code, claimed, 0, A.HITL_REVIEW, "UNAUTHORIZED_GHOST_FEE: " + ghost.detail,
                                  "GHOST_FEE", desc=desc, expected=exp)]
            elif code in overlap_codes:
                out = [self._line(code, claimed, 0, A.HITL_REVIEW,
                                  "OVERLAPPING_CLOCK: conflicting temporal claims; held for review.", "OVERLAP",
                                  desc=desc, expected=exp)]
            else:
                out = self._price(req, ev, code, claimed, desc, exp, facts, conflicts, alerts)
            for li in out:
                li.clause = self._clause(req.carrier_id, code, ev)
            per_charge[code] = out
            lines += out

        res = self._persist(req.movement_id, "ACTUAL", lines, alerts, expected)
        for code, ls in per_charge.items():
            if any(li.recommended_action in (A.APPROVE, A.PARTIAL_APPROVE) for li in ls):
                self.s.add(AuditRecord(hash=hashes[code], carrier_id=req.carrier_id, bol_number=req.bol_number,
                                       charge_code=code, reference=req.container_or_job_id, status="APPROVED",
                                       movement_id=req.movement_id))
        if req.invoice_number and not blocked:
            self.s.add(InvoiceRecord(carrier_id=req.carrier_id, invoice_number=req.invoice_number,
                                     movement_id=req.movement_id))
        self._upsert_cases(req.movement_id, per_charge, hashes, ev.location)
        self.s.flush()
        log.info("actual_settled", extra={"movement": req.movement_id, "action": res.overall_action.value})
        return res

    def _upsert_cases(self, movement_id: str, per_charge, hashes, location: Optional[str] = None) -> None:
        for code, ls in per_charge.items():
            worst = max(ls, key=lambda li: self.policies[li.reason_code].severity)
            pol = self.policies[worst.reason_code]
            vals = dict(claim_hash=hashes[code], claimed=money(sum(li.amount_claimed for li in ls)),
                        supported=money(sum(li.amount_supported for li in ls)), status=pol.initial_status,
                        reason_code=worst.reason_code, decision_owner=pol.decision_owner, next_action=pol.next_action,
                        location=location)
            case = self.s.query(Case).filter_by(movement_id=movement_id, charge_code=code).first()
            if case:
                for k, v in vals.items():
                    setattr(case, k, v)
            else:
                case = Case(movement_id=movement_id, charge_code=code, **vals)
                self.s.add(case)
            self.s.flush()
            emit_case_outcome(self.s, case, [li.model_dump(mode="json") for li in ls])

    def _price(self, req, ev, code, claimed, desc, exp, facts, conflicts, alerts) -> List[LineItemReconciliation]:
        def one(supported, action, findings, reason="NONE", refs=None, line_desc=None, gf=None, expected=exp):
            return [self._line(code, claimed, supported, action, findings, reason, refs, line_desc or desc, gf, expected)]

        carrier = req.carrier_id
        if code == "FSC":
            f = ev.fuel
            if not f:
                return one(0, A.REQUEST_MISSING_PROOF, "Missing ship date / miles to verify fuel peg.", "MISSING_EVIDENCE")
            t, price = self.terms(carrier, code, f.ship_date), diesel_price(self.s, f.ship_date)
            if t is None:
                return one(0, A.HITL_REVIEW, "No FSC contract terms for the ship date.", "MISSING_CONTRACT")
            if price is None:
                return one(0, A.REQUEST_MISSING_PROOF, "No DOE diesel index on file for the ship date.", "MISSING_EVIDENCE")
            rate = fsc_per_mile(price, t["base_price"], t["mpg"], self.cfg.fsc_rate_decimals)
            ctx = f"week of {prevailing_monday(f.ship_date)}: ${rate}/mi x {f.miles} mi"
            sup, alert = check_fuel_peg(rate * f.miles, claimed, self.cfg.money_tolerance, ctx)
            if alert:
                alerts.append(alert)
            return one(sup, self._by_amount(claimed, sup)[0],
                       alert.detail if alert else f"Matches prevailing Monday DOE index ({ctx}).",
                       "FUEL_PEG" if alert else "NONE")
        if code == "LH":
            if exp is None:
                return one(0, A.REQUEST_MISSING_PROOF, "No provisional invoice on file to compare linehaul against.",
                           "MISSING_EVIDENCE")
            action, reason = self._by_amount(claimed, exp)
            return one(exp, action, f"Provisional linehaul ${exp:.2f}.", reason)

        t = self._terms_for(carrier, code, ev)
        if t is not None:
            absent = [p for p in t.get("required_proofs", []) if p not in ev.proofs]
            if absent:
                return one(0, A.REQUEST_MISSING_PROOF, "Missing proof: " + ", ".join(absent) + ".", "MISSING_EVIDENCE")
        clk, err = self._clock(code, carrier, ev)
        group = next((g for g, c in GROUP_CHARGE.items() if c == code), None)
        gfacts = facts.get(group, []) if group else []
        if code == "DEM" and ev.demurrage:
            gfacts = [f"Pickup attempt {a.date} · {a.outcome.value}" + (f" · ref {a.reference}" if a.reference else "")
                      for a in ev.demurrage.pickup_attempts]
        if err:
            return one(0, A.HITL_REVIEW, err, "MISSING_CONTRACT", gf=gfacts)
        if clk is None:
            if t is None:
                return one(0, A.HITL_REVIEW, "No contract terms on file for this charge: contract review needed.",
                           "MISSING_CONTRACT")
            if "flat_amount" in t:
                action, reason = self._by_amount(claimed, t["flat_amount"])
                return one(t["flat_amount"], action, f"Contracted flat fee ${t['flat_amount']:.2f}.", reason)
            return one(0, A.REQUEST_MISSING_PROOF, "No operational evidence supplied for this charge.", "MISSING_EVIDENCE")
        if clk.status == ClockStatus.MISSING_PROOF:
            text = "Missing proof: " + ", ".join(clk.missing_proof) + ". " + " ".join(clk.notes)
            if conflicts:
                text += " " + " ".join(conflicts)
            return one(0, A.REQUEST_MISSING_PROOF, text, "CONFLICTING_EVIDENCE" if conflicts else "MISSING_EVIDENCE",
                       gf=gfacts)

        note = " ".join(n for n in clk.notes if not n.endswith("supported.") and "flagged" not in n and "held back" not in n)
        if conflicts:
            note += " Note: " + " ".join(conflicts)
        if code == "DET":
            _, unresolved = self._det_exceptions(carrier, ev)
            rate = (t or {}).get("hourly_rate", 0)
            ex_amt = min(round(sum(m for _, m, _, _ in unresolved) / 60 * rate, 2), clk.amount_supported, claimed)
            if unresolved and ex_amt > 0:
                text = "; ".join(f"{c} ({m} min, responsibility {r}: {w})" for c, m, r, w in unresolved)
                main = claimed - ex_amt
                sup = min(clk.amount_supported - ex_amt, main)
                out = []
                if main > self.cfg.money_tolerance:
                    act, rsn = self._by_amount(main, sup)
                    out.append(self._line(code, main, sup, act, note.strip(), rsn, clk.proof_references, desc, gfacts, exp))
                out.append(self._line(
                    code, ex_amt, 0, A.HITL_REVIEW,
                    f"${ex_amt:.2f} depends on claimed exception(s): {text}. An operational cause is not a financial "
                    "finding; the contract and evidence decide responsibility.",
                    "EXCEPTION_REVIEW", clk.proof_references, f"{desc or self.catalog[code].description} (exception review)",
                    gfacts, None))
                return out
        if clk.amount_disputed > 0 or clk.amount_held > 0:  # Example 2: appointment-constrained days go to dispute review, never auto-removed
            review = min(clk.amount_disputed, claimed)
            held_amt = min(clk.amount_held, claimed - review)
            main_claimed = claimed - review - held_amt
            out: List[LineItemReconciliation] = []
            if main_claimed > self.cfg.money_tolerance:
                sup = min(clk.amount_supported, main_claimed)
                action, reason = self._by_amount(main_claimed, sup)
                out.append(self._line(code, main_claimed, sup, action, note.strip(), reason, clk.proof_references,
                                      desc, gfacts, exp))
            base = desc or self.catalog[code].description
            if held_amt > 0:
                out.append(self._line(
                    code, held_amt, 0, A.REQUEST_MISSING_PROOF,
                    f"${held_amt:.2f} relates to claimed holds that cannot be verified from core terminal/customs events; "
                    "disputed days stay open until the hold record is obtained.",
                    "HOLD_UNVERIFIED", clk.proof_references, f"{base} (unverified hold days)", gfacts, None))
            if review > 0:
                out.append(self._line(
                code, review, 0, A.HITL_REVIEW,
                f"${review:.2f} relates to days with documented zero pickup appointments; proposed for dispute "
                "review (contract, terminal practice and alternatives must be assessed). Not auto-removed.",
                "APPOINTMENT_CONSTRAINED", clk.proof_references, f"{base} (appointment-constrained days)", gfacts, None))
            return out
        action, reason = self._by_amount(claimed, min(clk.amount_supported, claimed))
        return one(clk.amount_supported, action, note.strip(), reason, clk.proof_references, gf=gfacts)
