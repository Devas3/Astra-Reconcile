"""ORM tables. Business reference data (catalog, contracts, conflicts, fuel index, scenarios) lives here."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ChargeCatalog(Base):
    __tablename__ = "charge_catalog"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    description: Mapped[str] = mapped_column(String(200))
    requires_authorization: Mapped[bool] = mapped_column(Boolean, default=False)  # ghost-fee guard applies


class ConflictRule(Base):
    __tablename__ = "conflict_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_a: Mapped[str] = mapped_column(ForeignKey("charge_catalog.code"))
    code_b: Mapped[str] = mapped_column(ForeignKey("charge_catalog.code"))
    __table_args__ = (UniqueConstraint("code_a", "code_b"),)


class Contract(Base):
    """Rate-Con / Work Order term set per carrier and charge code. A row == pre-authorization."""
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    carrier_id: Mapped[str] = mapped_column(String(64), index=True)
    charge_code: Mapped[str] = mapped_column(ForeignKey("charge_catalog.code"))
    terms: Mapped[dict] = mapped_column(JSON)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    clause_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # tariff / agreement clause cited in evidence packs
    clause_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    __table_args__ = (UniqueConstraint("carrier_id", "charge_code", "effective_from"),)


class FuelIndexWeek(Base):
    __tablename__ = "fuel_index"
    week_start: Mapped[date] = mapped_column(Date, primary_key=True)  # Monday publication date
    price: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))  # EIA_API | SEED


class AuditRecord(Base):
    __tablename__ = "audit_records"
    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    carrier_id: Mapped[str] = mapped_column(String(64))
    bol_number: Mapped[str] = mapped_column(String(64))
    charge_code: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # APPROVED | PAID
    movement_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class FraudAlertRow(Base):
    __tablename__ = "fraud_alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    movement_id: Mapped[str] = mapped_column(String(64), index=True)
    alert_type: Mapped[str] = mapped_column(String(40), index=True)
    charge_code: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Settlement(Base):
    __tablename__ = "settlements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    movement_id: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(16))
    proof_id: Mapped[str] = mapped_column(String(32), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (Index("ix_settlement_movement_stage", "movement_id", "stage"),)


class Scenario(Base):
    __tablename__ = "scenarios"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(24))
    repeat: Mapped[int] = mapped_column(Integer, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    request: Mapped[dict] = mapped_column(JSON)  # templated ActualRequest ({{DATE-n}}, {{RUN}} tokens)


class EventType(Base):
    """Which evidence field an event feeds, and whether it closes the clock (triggers settlement)."""
    __tablename__ = "event_types"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    description: Mapped[str] = mapped_column(String(200))
    evidence_group: Mapped[str] = mapped_column(String(24))  # tl | service
    evidence_field: Mapped[str] = mapped_column(String(48))
    closes_clock: Mapped[bool] = mapped_column(Boolean, default=False)


class SourceReliability(Base):
    """Per event type, which sources are acceptable and how they rank (1 = most reliable)."""
    __tablename__ = "source_reliability"
    event_type: Mapped[str] = mapped_column(ForeignKey("event_types.code"), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)


class MovementEvent(Base):
    __tablename__ = "movement_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    movement_id: Mapped[str] = mapped_column(String(64), index=True)
    carrier_id: Mapped[str] = mapped_column(String(64))
    bol_number: Mapped[str] = mapped_column(String(64))
    container_or_job_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(ForeignKey("event_types.code"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # normalized UTC
    original_value: Mapped[str] = mapped_column(String(64))  # as received, with its local zone
    local_tz: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32))
    recorded_by: Mapped[str] = mapped_column(String(64), default="system")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    superseded_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class ActionPolicy(Base):
    """Maps a reason code to the decision owner, the next action and the case's starting status."""
    __tablename__ = "action_policies"
    reason_code: Mapped[str] = mapped_column(String(40), primary_key=True)
    decision_owner: Mapped[str] = mapped_column(String(120))
    next_action: Mapped[str] = mapped_column(Text)
    initial_status: Mapped[str] = mapped_column(String(32))
    severity: Mapped[int] = mapped_column(Integer)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    permission: Mapped[str] = mapped_column(String(48), primary_key=True)


class DecisionType(Base):
    __tablename__ = "decision_types"
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    permission: Mapped[str] = mapped_column(String(48))
    resulting_status: Mapped[str] = mapped_column(String(32))
    requires_reason: Mapped[bool] = mapped_column(Boolean, default=True)
    needs_amount: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_reference: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_from: Mapped[list] = mapped_column(JSON)  # case statuses this decision may follow ([] = any)


class Case(Base):
    """One workable item per movement + charge code."""
    __tablename__ = "cases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    movement_id: Mapped[str] = mapped_column(String(64), index=True)
    charge_code: Mapped[str] = mapped_column(String(32))
    claim_hash: Mapped[str] = mapped_column(String(64))
    claimed: Mapped[float] = mapped_column(Float)
    supported: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32))
    reason_code: Mapped[str] = mapped_column(String(40))
    decision_owner: Mapped[str] = mapped_column(String(120))
    next_action: Mapped[str] = mapped_column(Text)
    location: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    agreed_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # what carrier and we agreed
    paid_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    paid_reference: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    __table_args__ = (UniqueConstraint("movement_id", "charge_code"),)


class AuditLog(Base):
    """Every manual change: who, what changed from/to, why, when."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    field: Mapped[str] = mapped_column(String(48))
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class InvoiceRecord(Base):
    """Carrier invoice numbers seen, so a repeated invoice number is caught."""
    __tablename__ = "invoice_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    carrier_id: Mapped[str] = mapped_column(String(64))
    invoice_number: Mapped[str] = mapped_column(String(64))
    movement_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    __table_args__ = (UniqueConstraint("carrier_id", "invoice_number"),)


class NonWorkingDay(Base):
    """Holiday / closed-day calendar per facility, terminal or ramp (location code)."""
    __tablename__ = "non_working_days"
    location: Mapped[str] = mapped_column(String(64), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))


class Document(Base):
    """Evidence attachments (appointment confirmation, gate record, tariff extract...)."""
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    movement_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    filename: Mapped[str] = mapped_column(String(200))
    storage_path: Mapped[str] = mapped_column(String(400))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    uploaded_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class IntegrationSource(Base):
    """Inbound adapter: maps a source system's payload shape onto our movement events."""
    __tablename__ = "integration_sources"
    name: Mapped[str] = mapped_column(String(48), primary_key=True)
    description: Mapped[str] = mapped_column(String(200))
    mapping: Mapped[dict] = mapped_column(JSON)
    auth_env: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # env var holding the shared secret
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class IntegrationTarget(Base):
    """Outbound destination (finance / ERP, carrier workflow, TMS) and which outcomes it receives."""
    __tablename__ = "integration_targets"
    name: Mapped[str] = mapped_column(String(48), primary_key=True)
    description: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(400))
    subscribes: Mapped[list] = mapped_column(JSON)  # outcome kinds, e.g. ["SETTLEMENT_READY", "DISPUTE_RAISED"]
    auth_env: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # env var holding the bearer token
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class OutboxMessage(Base):
    """Transactional outbox: messages are written with the business change, delivered separately with retries."""
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target: Mapped[str] = mapped_column(String(48), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    movement_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)  # PENDING | SENT | FAILED
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class StatusOutcome(Base):
    """Which outbound outcome a case status triggers."""
    __tablename__ = "status_outcomes"
    status: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
