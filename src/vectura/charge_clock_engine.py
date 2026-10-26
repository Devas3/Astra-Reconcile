"""Deterministic multi-modal charge clock engine and standardized output schema.

All math here is plain arithmetic on timestamps/dates -- no LLMs involved.
Money is computed with Decimal and exposed as rounded floats.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Standardized 4-field output schema (Module 3)
# --------------------------------------------------------------------------- #


class ReconciliationAction(str, Enum):
    APPROVE = "APPROVE"
    PARTIAL_APPROVE = "PARTIAL_APPROVE"
    REQUEST_MISSING_PROOF = "REQUEST_MISSING_PROOF"
    HITL_REVIEW = "HITL_REVIEW"


class LineItemReconciliation(BaseModel):
    charge_code: str
    description: str
    amount_claimed: float
    amount_supported: float
    variance: float
    recommended_action: ReconciliationAction
    findings: str
    proof_references: List[str] = Field(default_factory=list)
    # Product-document extensions (optional, so the 4-field core stays intact)
    reason_code: str = "NONE"  # why supported differs from claimed
    decision_owner: str = ""
    next_action: str = ""
    evidence_facts: List[str] = Field(default_factory=list)  # every relevant fact, with source
    provisional_amount: Optional[float] = None  # pre-bill expected amount (provisional / event proposal)
    clause: Optional[str] = None  # contract / tariff clause the amount was calculated under


class ReconciliationResult(BaseModel):
    movement_id: str
    stage: str  # "PROVISIONAL" (origin departure) or "ACTUAL" (destination gate-out)
    total_claimed: float
    total_supported: float
    total_variance: float
    overall_action: ReconciliationAction
    fraud_alerts: List[str] = Field(default_factory=list)
    line_items: List[LineItemReconciliation] = Field(default_factory=list)
    audit_proof_pack_id: Optional[str] = None
    provisional_total: Optional[float] = None
    variance_vs_provisional: Optional[float] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class RoundingRule(str, Enum):
    EXACT = "EXACT"
    CEIL_15_MIN = "CEIL_15_MIN"
    CEIL_30_MIN = "CEIL_30_MIN"
    NEAREST_HOUR = "NEAREST_HOUR"


def money(value) -> float:
    """Round to cents (half-up) and return a float."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def apply_rounding(minutes: float, rule: RoundingRule) -> int:
    """Apply a contractual rounding rule to billable minutes."""
    if minutes <= 0:
        return 0
    if rule == RoundingRule.EXACT:
        return int(round(minutes))
    if rule in (RoundingRule.CEIL_15_MIN, RoundingRule.CEIL_30_MIN):
        step = 15 if rule == RoundingRule.CEIL_15_MIN else 30
        return int(-(-minutes // step) * step)
    # NEAREST_HOUR: half-up
    return int((Decimal(str(minutes)) / 60).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * 60)


class StartRule(str, Enum):
    APPOINTMENT_IF_ON_TIME = "APPOINTMENT_IF_ON_TIME"
    CHECK_IN = "CHECK_IN"
    DOCK_START = "DOCK_START"


class ClockStatus(str, Enum):
    OK = "OK"
    MISSING_PROOF = "MISSING_PROOF"


class ClockResult(BaseModel):
    """Common result of every clock evaluator."""

    charge_code: str
    status: ClockStatus = ClockStatus.OK
    amount_supported: float = 0.0
    amount_disputed: float = 0.0  # priced by the clock but flagged for dispute (e.g. no appointments)
    amount_held: float = 0.0  # priced days held back because a terminal hold could not be verified
    billable_units: float = 0.0  # minutes / days / hours depending on the charge
    unit: str = "minutes"
    clock_start: Optional[datetime] = None
    clock_end: Optional[datetime] = None
    missing_proof: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    proof_references: List[str] = Field(default_factory=list)
    protection_forfeited: bool = False


# --------------------------------------------------------------------------- #
# 1. Truckload detention
# --------------------------------------------------------------------------- #


class TLDetentionInput(BaseModel):
    appointment_time: Optional[datetime] = None
    arrival_time: Optional[datetime] = None  # GPS / gate
    app_arrival_time: Optional[datetime] = None  # carrier-reported (driver app); never trusted alone
    check_in_time: Optional[datetime] = None  # WMS
    dock_start_time: Optional[datetime] = None
    release_time: Optional[datetime] = None  # gate-out
    start_rule: StartRule  # which event the contract says starts the waiting clock
    excluded_minutes: int = 0  # contract-recognised exception time (with proof) removed from the clock
    free_time_minutes: int
    hourly_rate: float
    rounding_rule: RoundingRule


def evaluate_tl_detention(inp: TLDetentionInput) -> ClockResult:
    """Truckload detention clock; the contract's start_rule decides which event starts it.

    APPOINTMENT_IF_ON_TIME: arrival <= appointment starts at the appointment; a late arrival forfeits
    appointment protection and starts at dock start (or completed check-in).
    CHECK_IN / DOCK_START: the clock starts at that facility-recorded event.
    Missing facility proof is never guessed or replaced by GPS / app times.
    """
    res = ClockResult(charge_code="DET")
    rule = inp.start_rule
    missing: List[str] = []
    if rule == StartRule.APPOINTMENT_IF_ON_TIME:
        if inp.appointment_time is None:
            missing.append("appointment_time")
        if inp.arrival_time is None:
            missing.append("arrival_time (GPS/gate)")
    if inp.check_in_time is None and rule in (StartRule.APPOINTMENT_IF_ON_TIME, StartRule.CHECK_IN):
        missing.append("warehouse check-in record (WMS)")
    if rule == StartRule.DOCK_START and inp.dock_start_time is None:
        missing.append("dock start record")
    if inp.release_time is None:
        missing.append("release_time (gate-out)")
    if missing:
        res.status = ClockStatus.MISSING_PROOF
        res.missing_proof = missing
        res.notes.append(
            f"Contract clock rule {rule.value}; evidence incomplete, clock not started. "
            "No timestamp was selected or guessed.")
        return res

    if rule == StartRule.CHECK_IN:
        start = inp.check_in_time
        res.notes.append("Contract starts waiting time at completed check-in.")
    elif rule == StartRule.DOCK_START:
        start = inp.dock_start_time
        res.notes.append("Contract starts waiting time at dock start.")
    elif inp.arrival_time <= inp.appointment_time:
        start = inp.appointment_time
        res.notes.append("Arrived on/before appointment: clock starts at appointment time.")
    else:
        start = inp.dock_start_time or inp.check_in_time
        res.protection_forfeited = True
        res.notes.append(
            "Late arrival: appointment protection forfeited; clock starts at "
            + ("dock start." if inp.dock_start_time else "completed check-in.")
        )
    res.clock_start, res.clock_end = start, inp.release_time
    elapsed = max(0.0, (inp.release_time - start).total_seconds() / 60)
    billable = max(0.0, elapsed - inp.free_time_minutes - inp.excluded_minutes)
    rounded = apply_rounding(billable, inp.rounding_rule)
    res.billable_units = rounded
    res.amount_supported = money(Decimal(rounded) / 60 * Decimal(str(inp.hourly_rate)))
    res.notes.append(
        f"Elapsed {elapsed:.0f} min - free {inp.free_time_minutes} - excepted {inp.excluded_minutes} = {billable:.0f} billable "
        f"-> {rounded} min ({inp.rounding_rule.value}) @ ${inp.hourly_rate:.2f}/hr."
    )
    res.proof_references = [f"clock_start:{start.isoformat()}", f"release:{inp.release_time.isoformat()}"]
    if inp.check_in_time:
        res.proof_references.insert(0, f"check_in:{inp.check_in_time.isoformat()}")
    if inp.arrival_time:
        res.proof_references.insert(0, f"arrival:{inp.arrival_time.isoformat()}")
    return res


# --------------------------------------------------------------------------- #
# 2-4. Ocean demurrage / detention / storage (date-based)
# --------------------------------------------------------------------------- #


class RateTier(BaseModel):
    from_day: int
    to_day: Optional[int] = None  # None = open ended
    rate: float


def _tier_rate(tiers: List[RateTier], day_no: int) -> float:
    for t in tiers:
        if day_no >= t.from_day and (t.to_day is None or day_no <= t.to_day):
            return t.rate
    return tiers[-1].rate if tiers else 0.0


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


class DemurrageInput(BaseModel):
    discharge_date: date
    available_date: date
    last_free_day: date
    gate_out_date: Optional[date] = None
    daily_rate_tiers: List[RateTier]
    terminal_closed_dates: List[date] = Field(default_factory=list)
    documented_no_appointment_dates: List[date] = Field(default_factory=list)
    verified_hold_dates: List[date] = Field(default_factory=list)  # verified terminal/customs holds: clock paused
    unverified_hold_dates: List[date] = Field(default_factory=list)  # claimed holds with no core record: held for review


def evaluate_ocean_demurrage(inp: DemurrageInput) -> ClockResult:
    """Container demurrage inside the terminal.

    Day numbering counts from the available date (day 1). Days after the LFD are
    chargeable. Terminal-closed days are removed outright; days with documented
    zero pickup appointments are priced but flagged for dispute review.
    """
    res = ClockResult(charge_code="DEM", unit="days")
    if inp.gate_out_date is None:
        res.status = ClockStatus.MISSING_PROOF
        res.missing_proof = ["gate_out_date"]
        return res
    supported = Decimal(0)
    disputed = Decimal(0)
    held = Decimal(0)
    days = 0
    for d in _daterange(inp.last_free_day + timedelta(days=1), inp.gate_out_date):
        if d in inp.terminal_closed_dates:
            res.notes.append(f"{d}: terminal closed, excluded.")
            continue
        day_no = (d - inp.available_date).days + 1
        rate = Decimal(str(_tier_rate(inp.daily_rate_tiers, day_no)))
        if d in inp.verified_hold_dates:
            res.notes.append(f"{d}: verified terminal hold, clock paused, excluded.")
            continue
        if d in inp.unverified_hold_dates:
            held += rate
            res.notes.append(f"{d}: day {day_no} ${rate} held back - hold cannot be verified from core events.")
            continue
        if d in inp.documented_no_appointment_dates:
            disputed += rate
            res.notes.append(f"{d}: day {day_no} ${rate} flagged - no pickup appointments offered.")
        else:
            supported += rate
            days += 1
            res.notes.append(f"{d}: day {day_no} ${rate} supported.")
    res.billable_units = days
    res.amount_supported = money(supported)
    res.amount_disputed = money(disputed)
    res.amount_held = money(held)
    res.proof_references = [f"lfd:{inp.last_free_day}", f"gate_out:{inp.gate_out_date}"]
    return res


class DetentionInput(BaseModel):
    gate_out_date: date
    empty_return_date: Optional[date] = None
    equipment_free_days: int
    daily_rate: float
    depot_congestion_hold_dates: List[date] = Field(default_factory=list)


def evaluate_ocean_detention(inp: DetentionInput) -> ClockResult:
    """Equipment detention outside the terminal (gate-out -> empty return)."""
    res = ClockResult(charge_code="OCN_DET", unit="days")
    if inp.empty_return_date is None:
        res.status = ClockStatus.MISSING_PROOF
        res.missing_proof = ["empty_return_date"]
        return res
    days = 0
    for d in _daterange(inp.gate_out_date + timedelta(days=1), inp.empty_return_date):
        if (d - inp.gate_out_date).days <= inp.equipment_free_days:
            continue
        if d in inp.depot_congestion_hold_dates:
            res.notes.append(f"{d}: depot congestion hold, excluded.")
            continue
        days += 1
    res.billable_units = days
    res.amount_supported = money(Decimal(days) * Decimal(str(inp.daily_rate)))
    return res


class StorageInput(BaseModel):
    storage_start_date: date
    pickup_date: Optional[date] = None
    free_days: int
    daily_rate: float
    closed_dates: List[date] = Field(default_factory=list)


def evaluate_storage(inp: StorageInput) -> ClockResult:
    """Terminal/port/air storage: a physical space fee, independent of equipment demurrage.

    Free days cover [start, start + free_days - 1]; charging begins the day after.
    """
    res = ClockResult(charge_code="STOR", unit="days")
    if inp.pickup_date is None:
        res.status = ClockStatus.MISSING_PROOF
        res.missing_proof = ["pickup_date"]
        return res
    first_charge = inp.storage_start_date + timedelta(days=inp.free_days)
    days = sum(1 for d in _daterange(first_charge, inp.pickup_date) if d not in inp.closed_dates)
    res.billable_units = days
    res.amount_supported = money(Decimal(days) * Decimal(str(inp.daily_rate)))
    return res


# --------------------------------------------------------------------------- #
# 5. Service track (vessel washing / cleaning / brooming)
# --------------------------------------------------------------------------- #


class ServiceInput(BaseModel):
    job_id: str
    technician_check_in: Optional[datetime] = None
    customer_otp_sign_off: Optional[datetime] = None
    base_package_hours: float
    base_package_fee: float
    overtime_hourly_rate: float
    authorized_materials_cost: float = 0.0


def evaluate_service(inp: ServiceInput) -> ClockResult:
    """Service job: base fee + overtime beyond package hours + authorized materials."""
    res = ClockResult(charge_code="SVC", unit="hours")
    missing = []
    if inp.technician_check_in is None:
        missing.append("technician_check_in")
    if inp.customer_otp_sign_off is None:
        missing.append("customer_otp_sign_off")
    if missing:
        res.status = ClockStatus.MISSING_PROOF
        res.missing_proof = missing
        return res
    res.clock_start, res.clock_end = inp.technician_check_in, inp.customer_otp_sign_off
    hours = Decimal(str((inp.customer_otp_sign_off - inp.technician_check_in).total_seconds())) / 3600
    overtime = max(Decimal(0), hours - Decimal(str(inp.base_package_hours)))
    total = (
        Decimal(str(inp.base_package_fee))
        + overtime * Decimal(str(inp.overtime_hourly_rate))
        + Decimal(str(inp.authorized_materials_cost))
    )
    res.billable_units = float(hours)
    res.amount_supported = money(total)
    res.notes.append(
        f"Actual {float(hours):.2f}h, package {inp.base_package_hours}h, overtime {float(overtime):.2f}h "
        f"@ ${inp.overtime_hourly_rate:.2f}; materials ${inp.authorized_materials_cost:.2f}."
    )
    res.proof_references = [
        f"job:{inp.job_id}",
        f"check_in:{inp.technician_check_in.isoformat()}",
        f"otp_sign_off:{inp.customer_otp_sign_off.isoformat()}",
    ]
    return res


# --------------------------------------------------------------------------- #
# Rail demurrage (placement / constructive placement -> release)
# --------------------------------------------------------------------------- #


class RailInput(BaseModel):
    placement_date: date
    constructive_placement_date: Optional[date] = None
    release_date: Optional[date] = None
    closed_dates: List[date] = Field(default_factory=list)  # non-working days per the tariff
    start_event: str  # PLACEMENT | CONSTRUCTIVE_PLACEMENT (contract decides)
    free_days: int
    daily_rate_tiers: List[RateTier]


def evaluate_rail_demurrage(inp: RailInput) -> ClockResult:
    """Railcar demurrage: free days from the contract's start event, closed days excluded, tiered rates."""
    res = ClockResult(charge_code="RAIL_DEM", unit="days")
    start = inp.constructive_placement_date if inp.start_event == "CONSTRUCTIVE_PLACEMENT" else inp.placement_date
    missing = []
    if start is None:
        missing.append("constructive placement record")
    if inp.release_date is None:
        missing.append("release_date")
    if missing:
        res.status = ClockStatus.MISSING_PROOF
        res.missing_proof = missing
        return res
    total, days = Decimal(0), 0
    for d in _daterange(start + timedelta(days=inp.free_days), inp.release_date):
        if d in inp.closed_dates:
            continue
        days += 1
        total += Decimal(str(_tier_rate(inp.daily_rate_tiers, days)))
    res.billable_units = days
    res.amount_supported = money(total)
    res.notes.append(f"{inp.start_event} {start}; {inp.free_days} free days; {days} chargeable day(s).")
    res.proof_references = [f"{inp.start_event.lower()}:{start}", f"release:{inp.release_date}"]
    return res


# --------------------------------------------------------------------------- #
# Intermodal ramp storage (ramp availability or notice -> pickup)
# --------------------------------------------------------------------------- #


class IntermodalInput(BaseModel):
    ramp_available_date: Optional[date] = None
    notice_date: Optional[date] = None
    pickup_date: Optional[date] = None
    closed_dates: List[date] = Field(default_factory=list)
    start_event: str  # AVAILABLE | NOTICE (the contract decides)
    free_days: int
    daily_rate_tiers: List[RateTier]


def evaluate_intermodal_storage(inp: IntermodalInput) -> ClockResult:
    """Ramp storage: free days run from the contract's start event (availability or notice)."""
    res = ClockResult(charge_code="IM_STOR", unit="days")
    start = inp.notice_date if inp.start_event == "NOTICE" else inp.ramp_available_date
    missing = []
    if start is None:
        missing.append("ramp notice record" if inp.start_event == "NOTICE" else "ramp availability record")
    if inp.pickup_date is None:
        missing.append("pickup record")
    if missing:
        res.status = ClockStatus.MISSING_PROOF
        res.missing_proof = missing
        return res
    total, days = Decimal(0), 0
    for d in _daterange(start + timedelta(days=inp.free_days), inp.pickup_date):
        if d in inp.closed_dates:
            continue
        days += 1
        total += Decimal(str(_tier_rate(inp.daily_rate_tiers, days)))
    res.billable_units = days
    res.amount_supported = money(total)
    res.notes.append(f"{inp.start_event.lower()} {start}; {inp.free_days} free days; {days} chargeable day(s).")
    res.proof_references = [f"{inp.start_event.lower()}:{start}", f"pickup:{inp.pickup_date}"]
    return res
