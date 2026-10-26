"""Product-document gaps: integrations, reference matching, holds, exceptions, calendar, clauses, outcomes, reports, admin."""
import httpx
import pytest

from vectura import integrations
from vectura.charge_clock_engine import ReconciliationAction as A
from vectura.config import get_settings
from vectura.models import IntegrationTarget, OutboxMessage
from vectura.rule_engine import Reconciler
from vectura.schemas import ActualRequest

API = "/api/v1/reconcile"
TL = {"appointment_time": "2025-03-10T09:00:00", "arrival_time": "2025-03-10T08:45:00",
      "check_in_time": "2025-03-10T08:55:00", "release_time": "2025-03-10T12:30:00"}


def run(session, evidence, charges, mid="G1", bol="GB1", ref="T-1", **kw):
    return Reconciler(session).actual(ActualRequest(
        movement_id=mid, bol_number=bol, carrier_id="CARR-7", container_or_job_id=ref,
        claimed_charges=charges, evidence=evidence, **kw))


# ---- 1. integrations ---------------------------------------------------------------------------------------
WMS = {"movement": "W1", "carrier": "CARR-7", "bol": "WB1", "trailer": "TR-1", "facility": "DC-CHI", "tz": "America/Chicago",
       "events": [{"event": "GATE_IN", "ts": "2025-03-10T08:45:00"}, {"event": "CHECKED_IN", "ts": "2025-03-10T08:55:00"},
                  {"event": "UNKNOWN_CODE", "ts": "2025-03-10T09:00:00"}, {"event": "GATE_OUT", "ts": "2025-03-10T12:30:00"}]}


def test_inbound_adapter_maps_source_payload(client):
    r = client.post(f"{API}/integrations/wms/events", json=WMS).json()
    assert r["accepted"] == 3 and r["skipped_unmapped"] == ["UNKNOWN_CODE"]
    ev = client.get(f"{API}/events/W1").json()
    assert {e["event_type"] for e in ev["events"]} == {"ARRIVAL", "CHECK_IN", "RELEASE"}
    assert "13:45Z" in " ".join(ev["facts"]["tl"])  # 08:45 Chicago (CDT, UTC-5) -> 13:45Z; facts show local + UTC
    assert client.post(f"{API}/integrations/nope/events", json=WMS).status_code == 404


def test_inbound_secret_enforced(client, monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "auth_enabled", True)
    monkeypatch.setattr(cfg, "api_keys", {"k": {"user": "a", "role": "analyst"}})
    monkeypatch.setenv("VECTURA_SRC_WMS_SECRET", "s3cret")
    assert client.post(f"{API}/integrations/wms/events", json=WMS).status_code == 403
    assert client.post(f"{API}/integrations/wms/events", json=WMS, headers={"X-Source-Secret": "s3cret"}).status_code == 200


def test_outbox_delivery_and_retry(session):
    session.get(IntegrationTarget, "finance_erp").enabled = True
    session.commit()
    run(session, {"tl": TL}, [{"charge_code": "DET", "amount": 112.5}], mid="O1", bol="O1")
    msgs = session.query(OutboxMessage).all()
    assert [m.kind for m in msgs] == ["SETTLEMENT_READY"] and msgs[0].payload["supported"] == 112.5
    calls = []

    def ok(request):
        calls.append(request)
        return httpx.Response(200)
    out = integrations.dispatch(session, client=httpx.Client(transport=httpx.MockTransport(ok)))
    assert out == {"sent": 1, "failed": 0} and calls[0].headers["Idempotency-Key"] == f"outbox-{msgs[0].id}"
    session.get(IntegrationTarget, "finance_erp").enabled = True
    run(session, {"tl": TL}, [{"charge_code": "DET", "amount": 112.5}], mid="O2", bol="O2")
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    for _ in range(get_settings().outbox_max_attempts):
        integrations.dispatch(session, client=bad)
    m = session.query(OutboxMessage).filter_by(movement_id="O2").one()
    assert m.status == "FAILED" and m.attempts == get_settings().outbox_max_attempts and "500" in m.last_error


# ---- 2. reference matching & invoice numbers ---------------------------------------------------------------
def test_reference_mismatch_blocks_pricing(client):
    client.post(f"{API}/integrations/wms/events", json=WMS)
    r = client.post(f"{API}/actual", json={"movement_id": "W1", "bol_number": "OTHER-BOL", "carrier_id": "CARR-7",
                                           "container_or_job_id": "TR-1", "claimed_charges": [{"charge_code": "DET", "amount": 50}]}).json()
    assert r["overall_action"] == "HITL_REVIEW" and r["total_supported"] == 0
    assert "REFERENCE_MISMATCH" in r["fraud_alerts"][0] and "BOL" in r["fraud_alerts"][0]


def test_duplicate_invoice_number(session):
    run(session, {"tl": TL}, [{"charge_code": "DET", "amount": 112.5}], mid="I1", bol="I1", invoice_number="INV-9")
    r = run(session, {"tl": TL}, [{"charge_code": "DET", "amount": 112.5}], mid="I2", bol="I2", invoice_number="INV-9")
    assert r.total_supported == 0 and any("DUPLICATE_INVOICE" in a for a in r.fraud_alerts)


# ---- 3. holds, 5. calendar ---------------------------------------------------------------------------------
DEM = {"discharge_date": "2025-03-10", "available_date": "2025-03-10", "last_free_day": "2025-03-14", "gate_out_date": "2025-03-18"}


def test_verified_hold_pauses_clock_unverified_is_held(session):
    base = run(session, {"demurrage": DEM}, [{"charge_code": "DEM", "amount": 1000}], mid="H0", bol="H0")
    assert base.line_items[0].amount_supported == 1000  # 15,16,17,18 at $250
    verified = run(session, {"demurrage": {**DEM, "holds": [{"kind": "CUSTOMS", "start_date": "2025-03-15", "end_date": "2025-03-16",
                                                             "reference": "CBP-1", "verified": True}]}},
                   [{"charge_code": "DEM", "amount": 1000}], mid="H1", bol="H1")
    assert verified.line_items[0].amount_supported == 500 and verified.line_items[0].variance == 500
    held = run(session, {"demurrage": {**DEM, "holds": [{"kind": "TERMINAL", "start_date": "2025-03-15", "end_date": "2025-03-16"}]}},
               [{"charge_code": "DEM", "amount": 1000}], mid="H2", bol="H2")
    main, hold = held.line_items
    assert main.amount_supported == 500 and hold.reason_code == "HOLD_UNVERIFIED"
    assert hold.recommended_action == A.REQUEST_MISSING_PROOF and hold.amount_claimed == 500


def test_facility_calendar_excludes_closed_day(session):
    r = run(session, {"location": "PORT-LA", "demurrage": DEM}, [{"charge_code": "DEM", "amount": 1000}], mid="K1", bol="K1")
    assert r.line_items[0].amount_supported == 750  # 2025-03-17 is a closed day for PORT-LA in the calendar


# ---- 4. contract exceptions ----------------------------------------------------------------------------------
def test_recognised_exception_with_proof_reduces_clock(session):
    exc = [{"code": "DOCK_NOT_READY_SHIPPER", "minutes": 30, "proof": "DOCK_LOG"}]
    r = run(session, {"tl": TL, "exceptions": exc, "proofs": ["DOCK_LOG"]}, [{"charge_code": "DET", "amount": 112.5}], mid="X1", bol="X1")
    assert r.line_items[0].amount_supported == 75.0 and "excepted 30" in r.line_items[0].findings  # 90-30=60 min


def test_unproven_or_undetermined_exception_goes_to_review(session):
    exc = [{"code": "FORCE_MAJEURE", "minutes": 60}]
    r = run(session, {"tl": TL, "exceptions": exc}, [{"charge_code": "DET", "amount": 112.5}], mid="X2", bol="X2")
    main, review = r.line_items
    assert review.reason_code == "EXCEPTION_REVIEW" and review.amount_claimed == 75.0 and review.recommended_action == A.HITL_REVIEW
    assert "not a financial finding" in review.findings and main.amount_supported == 37.5


# ---- 6. clause in evidence ------------------------------------------------------------------------------------
def test_line_cites_contract_clause(session):
    r = run(session, {"tl": TL}, [{"charge_code": "DET", "amount": 262.5}], mid="C1", bol="C1")
    assert r.line_items[0].clause.startswith("4.2:") and "two (2) free hours" in r.line_items[0].clause


def test_documents_attach_to_memo(client, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "document_dir", tmp_path)
    client.post(f"{API}/actual", json={"movement_id": "DOC1", "bol_number": "D", "carrier_id": "CARR-7", "container_or_job_id": "T",
                                       "claimed_charges": [{"charge_code": "DET", "amount": 262.5}], "evidence": {"tl": TL}})
    up = client.post(f"{API}/documents?movement_id=DOC1&kind=APPOINTMENT_CONFIRMATION", files={"file": ("appt.pdf", b"%PDF-1.4 test")})
    assert up.status_code == 200 and len(up.json()["sha256"]) == 64
    listing = client.get(f"{API}/documents?movement_id=DOC1").json()
    assert listing[0]["kind"] == "APPOINTMENT_CONFIRMATION"
    assert client.get(f"{API}/documents/{listing[0]['id']}/download").content == b"%PDF-1.4 test"
    assert client.post(f"{API}/dispute-memo", json={"movement_id": "DOC1"}).status_code == 200


# ---- 7. agreed / paid outcomes ---------------------------------------------------------------------------------
def test_agreed_then_paid_with_reference(client):
    client.post(f"{API}/actual", json={"movement_id": "P1", "bol_number": "P", "carrier_id": "CARR-7", "container_or_job_id": "T",
                                       "claimed_charges": [{"charge_code": "DET", "amount": 262.5}], "evidence": {"tl": TL}})
    d = lambda **k: client.post(f"{API}/decisions", json={"movement_id": "P1", "charge_code": "DET", **k})  # noqa: E731
    assert d(decision="DISPUTE", reason="gate proof").status_code == 200
    assert d(decision="AGREE_SETTLEMENT", reason="carrier accepted revised bill", amount=150).json()["supported"] == 150
    assert d(decision="MARK_PAID", amount=100, reference="PAY-1").status_code == 422  # differs from agreed 150
    assert d(decision="MARK_PAID", amount=150, reference="PAY-1").json()["status"] == "PAID"
    case = client.get(f"{API}/cases?movement_id=P1").json()[0]
    assert (case["agreed_amount"], case["paid_amount"], case["paid_reference"]) == (150, 150, "PAY-1")
    fields = {a["field"] for a in client.get(f"{API}/audit-log?entity_id=P1:DET").json()}
    assert {"agreed_amount", "paid_amount", "status"} <= fields


# ---- 8. manager report ---------------------------------------------------------------------------------------
def test_repeat_causes_by_facility(client):
    for i in range(2):
        client.post(f"{API}/actual", json={"movement_id": f"R{i}", "bol_number": f"R{i}", "carrier_id": "CARR-7", "container_or_job_id": "T",
                                           "claimed_charges": [{"charge_code": "DET", "amount": 262.5}],
                                           "evidence": {"location": "DC-CHI", "tl": TL}})
    rows = client.get(f"{API}/reports/repeat-causes?location=DC-CHI").json()
    assert rows[0]["reason_code"] == "OVER_CLAIM" and rows[0]["cases"] == 2 and rows[0]["repeated"] is True
    assert rows[0]["disputed_value"] == 300.0


# ---- 9. admin --------------------------------------------------------------------------------------------------
def test_admin_contract_versioning_and_permissions(client, session, monkeypatch):
    new = {"carrier_id": "CARR-7", "charge_code": "DET", "effective_from": "2025-03-01", "reason": "renegotiated rate",
           "terms": {"start_rule": "APPOINTMENT_IF_ON_TIME", "free_time_minutes": 60, "hourly_rate": 90.0, "rounding_rule": "EXACT"},
           "clause_ref": "4.2a", "clause_text": "Revised detention terms"}
    assert client.post(f"{API}/admin/contracts", json=new).status_code == 200
    rows = [r for r in client.get(f"{API}/admin/contracts?carrier_id=CARR-7").json() if r["charge_code"] == "DET"]
    assert [r["effective_to"] for r in rows] == ["2025-02-28", None]
    r = run(session, {"tl": TL}, [{"charge_code": "DET", "amount": 262.5}], mid="A1", bol="A1")  # 210-60=150 min @ $90 on the new version
    assert r.line_items[0].amount_supported == 225.0 and r.line_items[0].clause.startswith("4.2a")
    old = run(session, {"tl": {k: v.replace("03-10", "02-10") for k, v in TL.items()}}, [{"charge_code": "DET", "amount": 262.5}],
              mid="A2", bol="A2")  # a February movement still uses the old terms
    assert old.line_items[0].amount_supported == 112.5
    log = client.get(f"{API}/audit-log?entity_id=CARR-7:DET").json()
    assert any(a["reason"] == "renegotiated rate" for a in log)

    cfg = get_settings()
    monkeypatch.setattr(cfg, "auth_enabled", True)
    monkeypatch.setattr(cfg, "api_keys", {"k": {"user": "ann", "role": "analyst"}})
    assert client.get(f"{API}/admin/contracts", headers={"X-API-Key": "k"}).status_code == 403


def test_admin_role_and_rank_and_calendar(client):
    assert client.put(f"{API}/admin/roles/coordinator", json={"permissions": ["view"], "reason": "tighten"}).status_code == 200
    assert client.put(f"{API}/admin/source-reliability/ARRIVAL/GPS", json={"rank": 1, "reason": "gate feed unreliable"}).status_code == 200
    assert client.post(f"{API}/admin/calendar/PORT-LA", json={"days": {"2025-04-01": "Test holiday"}, "reason": "tariff update"}).json() == {"added": 1}
    assert client.put(f"{API}/admin/charges/NEWFEE", json={"description": "New fee", "requires_authorization": True, "reason": "added"}).status_code == 200


# ---- 10. intermodal / LTL --------------------------------------------------------------------------------------
def test_intermodal_storage_and_ltl_reconsignment(session):
    r = run(session, {"intermodal": {"notice_date": "2025-03-01", "pickup_date": "2025-03-06"}},
            [{"charge_code": "IM_STOR", "amount": 240}], mid="M1", bol="M1")
    assert r.line_items[0].amount_supported == 240.0  # free 03-01..03-02; charge 03-03..03-06 = 40+40+80+80
    miss = run(session, {"proofs": ["PROOF_OF_ATTEMPT"]}, [{"charge_code": "LTL_RECON", "amount": 120}], mid="M2", bol="M2")
    assert miss.line_items[0].recommended_action == A.REQUEST_MISSING_PROOF and "FACILITY_HOURS" in miss.line_items[0].findings
    ok = run(session, {"proofs": ["PROOF_OF_ATTEMPT", "FACILITY_HOURS"]}, [{"charge_code": "LTL_RECON", "amount": 120}], mid="M3", bol="M3")
    assert ok.line_items[0].recommended_action == A.APPROVE


def test_prevention_notification_queued(client, session):
    session.get(IntegrationTarget, "tms_alerts").enabled = True
    session.commit()
    client.post(f"{API}/events", json={"movement_id": "N1", "carrier_id": "CARR-7", "bol_number": "N", "container_or_job_id": "T",
                                       "events": [{"event_type": "APPOINTMENT", "occurred_at": "2025-03-10T09:00:00", "source": "SCHEDULER"}]})
    assert client.get(f"{API}/prevention?as_of=2025-03-10T10:00:00Z&notify=true").json()[0]["alert_type"] == "PREVENT_NO_ARRIVAL"
    assert client.get(f"{API}/outbox?status=PENDING").json()[0]["kind"] == "PREVENTION_ALERT"


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
def test_health(client, path):
    assert client.get(path).status_code == 200
