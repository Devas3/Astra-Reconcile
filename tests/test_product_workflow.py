"""Product-document behavior: evidence quality, contract clock rules, events, ownership, decisions, prevention."""
from datetime import date, datetime, timezone

import pytest

from vectura.charge_clock_engine import ReconciliationAction as A
from vectura.models import Contract
from vectura.rule_engine import Reconciler
from vectura.schemas import ActualRequest


def utc(h, m, day=10):
    return datetime(2025, 3, day, h, m, tzinfo=timezone.utc)


def run(session, evidence, charges, mid="P1", bol="PB1", ref="TRL-9", carrier="CARR-7", **kw):
    return Reconciler(session).actual(ActualRequest(
        movement_id=mid, bol_number=bol, carrier_id=carrier, container_or_job_id=ref,
        claimed_charges=charges, evidence=evidence, **kw))


def post_events(client, mid, events, ref="TRL-9", bol="PB1"):
    return client.post("/api/v1/reconcile/events", json={
        "movement_id": mid, "carrier_id": "CARR-7", "bol_number": bol, "container_or_job_id": ref, "events": events})


def ev(t, h, m, src, tz=None, day=10):
    return {"event_type": t, "occurred_at": f"2025-03-{day}T{h:02d}:{m:02d}:00", "source": src, "timezone": tz}


# ---- Example 3: conflicting / incomplete evidence ---------------------------------------------------------
def test_example3_shows_all_facts_and_never_picks_a_time(session):
    contract = session.query(Contract).filter_by(carrier_id="CARR-7", charge_code="DET").one()
    contract.terms = {**contract.terms, "start_rule": "CHECK_IN"}  # contract starts the clock at completed check-in
    session.commit()
    r = run(session, {"tl": {"appointment_time": "2025-03-10T09:00:00", "arrival_time": "2025-03-10T08:40:00",
                             "app_arrival_time": "2025-03-10T09:05:00", "release_time": "2025-03-10T13:00:00"}},
            [{"charge_code": "DET", "amount": 300}])
    li = r.line_items[0]
    assert li.recommended_action == A.REQUEST_MISSING_PROOF and li.amount_supported == 0
    assert li.reason_code == "CONFLICTING_EVIDENCE"
    joined = " | ".join(li.evidence_facts)
    assert "08:40" in joined and "09:05" in joined and "Facility check-in completed · NOT RECORDED" in joined
    assert "disagree by 25 min" in li.findings


def test_contract_defined_clock_start(session):
    contract = session.query(Contract).filter_by(carrier_id="CARR-7", charge_code="DET").one()
    contract.terms = {**contract.terms, "start_rule": "CHECK_IN"}
    session.commit()
    tl = {"appointment_time": "2025-03-10T09:00:00", "arrival_time": "2025-03-10T08:45:00",
          "check_in_time": "2025-03-10T09:30:00", "release_time": "2025-03-10T12:30:00"}
    r = run(session, {"tl": tl}, [{"charge_code": "DET", "amount": 100}])
    assert r.line_items[0].amount_supported == 75.0  # 09:30->12:30 = 180 min - 120 free = 60 min @ $75


# ---- Time zones --------------------------------------------------------------------------------------------
def test_naive_times_use_facility_zone_and_offsets_agree(session):
    base = {"appointment_time": "2025-03-10T09:00:00", "arrival_time": "2025-03-10T08:45:00",
            "check_in_time": "2025-03-10T08:55:00", "release_time": "2025-03-10T12:30:00"}
    ny = run(session, {"timezone": "America/New_York", "tl": base}, [{"charge_code": "DET", "amount": 262.5}], mid="Z1", bol="Z1")
    explicit = {k: v + "-04:00" for k, v in base.items()}  # DST began 2025-03-09: New York is UTC-4
    off = run(session, {"tl": explicit}, [{"charge_code": "DET", "amount": 262.5}], mid="Z2", bol="Z2")
    assert ny.line_items[0].amount_supported == off.line_items[0].amount_supported == 112.5


# ---- Provisional vs actual ---------------------------------------------------------------------------------
def test_actual_compares_with_provisional(client):
    client.post("/api/v1/reconcile/provisional", json={
        "movement_id": "V1", "bol_number": "V", "carrier_id": "CARR-7", "origin": "A", "destination": "B",
        "origin_departure": "2025-03-10T06:00:00", "miles": 100})
    r = client.post("/api/v1/reconcile/actual", json={
        "movement_id": "V1", "bol_number": "V", "carrier_id": "CARR-7", "container_or_job_id": "T",
        "claimed_charges": [{"charge_code": "LH", "amount": 300}, {"charge_code": "FSC", "amount": 40.8}],
        "evidence": {"fuel": {"ship_date": "2025-03-10", "miles": 100}}}).json()
    lh, fsc = r["line_items"]
    assert lh["provisional_amount"] == 250 and lh["amount_supported"] == 250 and lh["variance"] == 50
    assert fsc["provisional_amount"] == 40.8
    assert r["provisional_total"] == 290.8 and r["variance_vs_provisional"] == 0


# ---- Event store, reliability, corrections, proposals -----------------------------------------------------
def test_events_build_evidence_and_trigger_proposal(client):
    out = post_events(client, "E1", [
        ev("APPOINTMENT", 9, 0, "SCHEDULER"), ev("ARRIVAL", 8, 45, "GPS"), ev("CHECK_IN", 8, 55, "WMS"),
        ev("RELEASE", 12, 30, "GATE")]).json()
    prop = out["proposal"]
    assert prop["stage"] == "PROPOSED" and prop["line_items"][0]["amount_supported"] == 112.5
    # carrier bill arrives later with NO inline evidence: recorded events supply it
    r = client.post("/api/v1/reconcile/actual", json={
        "movement_id": "E1", "bol_number": "PB1", "carrier_id": "CARR-7", "container_or_job_id": "TRL-9",
        "claimed_charges": [{"charge_code": "DET", "amount": 262.5}]}).json()
    li = r["line_items"][0]
    assert (li["amount_supported"], li["variance"], li["provisional_amount"]) == (112.5, 150.0, 112.5)
    assert li["decision_owner"] and li["next_action"] and any("GPS" in f for f in li["evidence_facts"])


def test_unrecognized_source_rejected(client):
    r = post_events(client, "E2", [ev("CHECK_IN", 8, 55, "DRIVER_APP")])
    assert r.status_code == 422


def test_correction_keeps_original_and_audits(client):
    ids = post_events(client, "C1", [ev("CHECK_IN", 8, 55, "WMS")]).json()["event_ids"]
    r = client.post(f"/api/v1/reconcile/events/{ids[0]}/correct",
                    json={"new_occurred_at": "2025-03-10T09:10:00", "reason": "WMS clock skew"})
    assert r.status_code == 200
    listing = client.get("/api/v1/reconcile/events/C1").json()
    assert [e["superseded_by"] for e in listing["events"]] == [r.json()["new_event_id"], None]
    log = client.get(f"/api/v1/reconcile/audit-log?entity_id={ids[0]}").json()[0]
    assert log["old"].startswith("2025-03-10T08:55:00") and log["new"].startswith("2025-03-10T09:10:00")
    assert log["reason"] == "WMS clock skew" and log["actor"] == "local-dev"


# ---- Example 2 / owners ----------------------------------------------------------------------------------
def test_rail_and_ltl_rules(session):
    rail = {"placement_date": "2025-03-01", "constructive_placement_date": "2025-03-02", "release_date": "2025-03-07"}
    r = run(session, {"rail": rail}, [{"charge_code": "RAIL_DEM", "amount": 300}], mid="R1", bol="R1")
    # start 03-02, 2 free days -> charge 03-04..03-07 = 4 days: 75+75+75+150
    assert r.line_items[0].amount_supported == 300.0
    no_proof = run(session, {}, [{"charge_code": "LTL_REDEL", "amount": 85}], mid="L1", bol="L1")
    assert no_proof.line_items[0].recommended_action == A.REQUEST_MISSING_PROOF
    ok = run(session, {"proofs": ["PROOF_OF_ATTEMPT"]}, [{"charge_code": "LTL_REDEL", "amount": 85}], mid="L2", bol="L2")
    assert ok.line_items[0].recommended_action == A.APPROVE


def test_every_line_has_owner_and_next_action(session):
    r = run(session, {}, [{"charge_code": "LFT", "amount": 50}, {"charge_code": "ZZZ", "amount": 5}])
    assert all(li.decision_owner and li.next_action for li in r.line_items)
    assert {li.reason_code for li in r.line_items} == {"GHOST_FEE", "UNKNOWN_CHARGE"}


# ---- Cases, decisions, permissions -------------------------------------------------------------------------
def overcharge(client, mid="D1", bol="D1"):
    return client.post("/api/v1/reconcile/actual", json={
        "movement_id": mid, "bol_number": bol, "carrier_id": "CARR-7", "container_or_job_id": "T",
        "claimed_charges": [{"charge_code": "DET", "amount": 262.5}],
        "evidence": {"tl": {"appointment_time": "2025-03-10T09:00:00", "arrival_time": "2025-03-10T08:45:00",
                            "check_in_time": "2025-03-10T08:55:00", "release_time": "2025-03-10T12:30:00"}}})


def decide(client, decision, **kw):
    return client.post("/api/v1/reconcile/decisions", json={
        "movement_id": "D1", "charge_code": "DET", "decision": decision, **kw})


def test_case_lifecycle_and_audit(client):
    overcharge(client)
    case = client.get("/api/v1/reconcile/cases?movement_id=D1").json()[0]
    assert case["status"] == "CORRECTION_REQUESTED" and case["reason_code"] == "OVER_CLAIM"
    assert decide(client, "DISPUTE").status_code == 422  # reason required
    assert decide(client, "DISPUTE", reason="Carrier billed 3.5h; gate proof shows 1.5h").json()["status"] == "DISPUTED"
    assert decide(client, "WRITE_OFF", reason="goodwill", amount=150).status_code == 200  # admin holds large permission
    audit = client.get("/api/v1/reconcile/audit-log?entity_id=D1:DET").json()
    assert {a["field"] for a in audit} >= {"status", "written_off_amount"} and all(a["reason"] for a in audit)


def test_permissions_and_large_writeoff_rule(client, monkeypatch):
    from vectura.config import get_settings
    cfg = get_settings()
    monkeypatch.setattr(cfg, "auth_enabled", True)
    monkeypatch.setattr(cfg, "api_keys", {"k-an": {"user": "ann", "role": "analyst"},
                                          "k-mg": {"user": "max", "role": "manager"},
                                          "k-fin": {"user": "fay", "role": "finance"}})
    H = lambda k: {"X-API-Key": k}  # noqa: E731
    base = {"movement_id": "D1", "charge_code": "DET"}
    ok = client.post("/api/v1/reconcile/actual", headers=H("k-an"), json={
        **base, "bol_number": "D1", "carrier_id": "CARR-7", "container_or_job_id": "T",
        "claimed_charges": [{"charge_code": "DET", "amount": 262.5}],
        "evidence": {"tl": {"appointment_time": "2025-03-10T09:00:00", "arrival_time": "2025-03-10T08:45:00",
                            "check_in_time": "2025-03-10T08:55:00", "release_time": "2025-03-10T12:30:00"}}})
    assert ok.status_code == 200
    d = lambda k, **kw: client.post("/api/v1/reconcile/decisions", headers=H(k), json={**base, **kw})  # noqa: E731
    assert d("k-fin", decision="DISPUTE", reason="x").status_code == 403  # finance cannot dispute
    assert d("k-an", decision="DISPUTE", reason="gate proof").status_code == 200
    assert d("k-an", decision="WRITE_OFF", reason="goodwill", amount=150).status_code == 403  # > threshold: manager only
    assert d("k-mg", decision="WRITE_OFF", reason="goodwill", amount=150).status_code == 200
    assert d("k-fin", decision="MARK_PAID").status_code == 422  # not READY_TO_POST


def test_mark_paid_flow_blocks_resubmission(client):
    client.post("/api/v1/reconcile/actual", json={
        "movement_id": "D1", "bol_number": "D1", "carrier_id": "CARR-7", "container_or_job_id": "T",
        "claimed_charges": [{"charge_code": "DET", "amount": 112.5}],
        "evidence": {"tl": {"appointment_time": "2025-03-10T09:00:00", "arrival_time": "2025-03-10T08:45:00",
                            "check_in_time": "2025-03-10T08:55:00", "release_time": "2025-03-10T12:30:00"}}})
    assert decide(client, "MARK_PAID").json()["status"] == "PAID"
    assert any(a["field"] == "audit_record_status" for a in client.get("/api/v1/reconcile/audit-log?entity_id=D1:DET").json())


# ---- Prevention ---------------------------------------------------------------------------------------------
def test_prevention_alerts(client):
    post_events(client, "PV1", [ev("APPOINTMENT", 9, 0, "SCHEDULER"), ev("ARRIVAL", 8, 45, "GPS"),
                                ev("CHECK_IN", 8, 55, "WMS")], bol="PV1", ref="T1")
    post_events(client, "PV2", [ev("APPOINTMENT", 9, 0, "SCHEDULER")], bol="PV2", ref="T2")
    q = lambda t: {a["movement_id"]: a for a in client.get(f"/api/v1/reconcile/prevention?as_of=2025-03-10T{t}:00Z").json()}  # noqa: E731
    near = q("10:45")  # 15 min of free time left
    assert near["PV1"]["alert_type"] == "PREVENT_APPROACHING" and near["PV1"]["minutes_remaining"] == pytest.approx(15)
    assert near["PV2"]["alert_type"] == "PREVENT_NO_ARRIVAL" and near["PV1"]["decision_owner"] == "Transport operations"
    assert q("11:30")["PV1"]["alert_type"] == "PREVENT_EXCEEDED"
    assert "PV1" not in q("09:30")  # plenty of free time left
    post_events(client, "PV1", [ev("RELEASE", 11, 0, "GATE")], bol="PV1", ref="T1")
    assert "PV1" not in q("11:30")  # clock closed: no alert


def test_ui_dates_unused(session):
    assert date(2025, 3, 10)  # placeholder keeps imports honest for future UI-date tests
