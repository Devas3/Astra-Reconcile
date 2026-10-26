"""API request models. Rates/free-time come from stored contracts, not from requests."""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import List, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from .config import get_settings
from .fraud_guardrails import TimeClaim


class ClaimedCharge(BaseModel):
    charge_code: str
    amount: float = Field(ge=0)
    description: Optional[str] = None
    receipt_ref: Optional[str] = None  # verified receipt attached


class TLEvidence(BaseModel):
    appointment_time: Optional[datetime] = None
    arrival_time: Optional[datetime] = None  # GPS / gate
    app_arrival_time: Optional[datetime] = None  # carrier-reported, never trusted alone
    check_in_time: Optional[datetime] = None  # WMS
    dock_start_time: Optional[datetime] = None
    release_time: Optional[datetime] = None  # gate-out


class AttemptOutcome(str, Enum):
    NO_SLOTS_OFFERED = "NO_SLOTS_OFFERED"
    SLOT_OFFERED_NOT_TAKEN = "SLOT_OFFERED_NOT_TAKEN"
    PICKED_UP = "PICKED_UP"


class PickupAttempt(BaseModel):
    date: date
    outcome: AttemptOutcome
    reference: Optional[str] = None  # appointment-system record id


class Hold(BaseModel):
    kind: str  # CUSTOMS, TERMINAL, LINE... as named by the terminal platform
    start_date: date
    end_date: date
    reference: Optional[str] = None
    verified: bool = False  # True only when a core terminal/customs record backs it


class ClaimedException(BaseModel):
    code: str  # must exist in the contract's exceptions to have any effect
    minutes: int = Field(default=0, ge=0)
    proof: Optional[str] = None  # document name; must also be listed in Evidence.proofs


class DemurrageEvidence(BaseModel):
    discharge_date: date
    available_date: date
    last_free_day: date
    gate_out_date: Optional[date] = None
    terminal_closed_dates: List[date] = Field(default_factory=list)
    documented_no_appointment_dates: List[date] = Field(default_factory=list)
    pickup_attempts: List[PickupAttempt] = Field(default_factory=list)
    holds: List[Hold] = Field(default_factory=list)

    @model_validator(mode="after")
    def _merge_attempts(self):
        extra = {a.date for a in self.pickup_attempts if a.outcome == AttemptOutcome.NO_SLOTS_OFFERED}
        self.documented_no_appointment_dates = sorted(set(self.documented_no_appointment_dates) | extra)
        return self


class DetentionEvidence(BaseModel):
    gate_out_date: date
    empty_return_date: Optional[date] = None
    depot_congestion_hold_dates: List[date] = Field(default_factory=list)


class StorageEvidence(BaseModel):
    storage_start_date: date
    pickup_date: Optional[date] = None
    closed_dates: List[date] = Field(default_factory=list)


class ServiceEvidence(BaseModel):
    job_id: str
    technician_check_in: Optional[datetime] = None
    customer_otp_sign_off: Optional[datetime] = None
    authorized_materials_cost: float = Field(default=0, ge=0)


class RailEvidence(BaseModel):
    placement_date: date
    constructive_placement_date: Optional[date] = None
    release_date: Optional[date] = None
    closed_dates: List[date] = Field(default_factory=list)


class IntermodalEvidence(BaseModel):
    ramp_available_date: Optional[date] = None
    notice_date: Optional[date] = None
    pickup_date: Optional[date] = None
    closed_dates: List[date] = Field(default_factory=list)


class FuelEvidence(BaseModel):
    ship_date: date
    miles: float = Field(gt=0)


def to_utc(dt: Optional[datetime], tz: str) -> Optional[datetime]:
    """Naive timestamps are local to `tz`; everything is compared in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc)


class Evidence(BaseModel):
    timezone: Optional[str] = None  # IANA zone of the facility for naive timestamps
    location: Optional[str] = None  # facility / terminal / ramp code: drives the calendar and facility reports
    proofs: List[str] = Field(default_factory=list)  # named documents attached (e.g. PROOF_OF_ATTEMPT)
    exceptions: List[ClaimedException] = Field(default_factory=list)
    intermodal: Optional[IntermodalEvidence] = None
    rail: Optional[RailEvidence] = None
    tl: Optional[TLEvidence] = None
    demurrage: Optional[DemurrageEvidence] = None
    detention: Optional[DetentionEvidence] = None
    storage: Optional[StorageEvidence] = None
    service: Optional[ServiceEvidence] = None
    fuel: Optional[FuelEvidence] = None
    time_claims: List[TimeClaim] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_times(self):
        self.timezone = self.timezone or get_settings().default_timezone
        for grp in (self.tl, self.service):
            if grp is not None:
                for name, val in grp.__dict__.items():
                    if isinstance(val, datetime):
                        setattr(grp, name, to_utc(val, self.timezone))
        for c in self.time_claims:
            c.start, c.end = to_utc(c.start, self.timezone), to_utc(c.end, self.timezone)
        return self


class ProvisionalRequest(BaseModel):
    movement_id: str
    bol_number: str
    carrier_id: str
    origin: str
    destination: str
    origin_departure: datetime
    miles: float = Field(gt=0)


class ActualRequest(BaseModel):
    movement_id: str
    bol_number: str
    carrier_id: str
    container_or_job_id: str
    claimed_charges: List[ClaimedCharge]
    invoice_number: Optional[str] = None
    evidence: Evidence = Field(default_factory=Evidence)
    use_recorded_events: bool = True  # fill gaps in `evidence` from the movement's recorded events
    dry_run: bool = False  # compute the full result but save nothing (live what-if preview)


class EventIn(BaseModel):
    event_type: str
    occurred_at: datetime  # with offset, or naive + timezone below
    timezone: Optional[str] = None
    source: str


class EventBatch(BaseModel):
    movement_id: str
    carrier_id: str
    bol_number: str
    container_or_job_id: str
    location: Optional[str] = None
    events: List[EventIn]


class CorrectionRequest(BaseModel):
    new_occurred_at: datetime
    timezone: Optional[str] = None
    reason: str = Field(min_length=3)


class DecisionRequest(BaseModel):
    movement_id: str
    charge_code: str
    decision: str  # a decision_types.code
    reason: Optional[str] = None
    amount: Optional[float] = Field(default=None, ge=0)  # WRITE_OFF / OVERRIDE_AMOUNT / AGREE_SETTLEMENT / MARK_PAID
    reference: Optional[str] = None  # payment or carrier-agreement reference


class MemoRequest(BaseModel):
    movement_id: str
