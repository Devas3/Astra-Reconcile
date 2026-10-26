"""Reconcile V3 tests: clocks, guardrails, settlement, API. Contract terms come from data/seed."""
from datetime import date, datetime

from vectura.charge_clock_engine import (
    DemurrageInput,
    RateTier,
    RoundingRule,
    ServiceInput,
    TLDetentionInput,
    apply_rounding,
    evaluate_ocean_demurrage,
    evaluate_service,
    evaluate_tl_detention,
)
from vectura.charge_clock_engine import (
    ReconciliationAction as A,
)
from vectura.rule_engine import Reconciler
from vectura.schemas import ActualRequest


def D(h, m, day=10):
    return datetime(2025, 3, day, h, m)


TL = dict(appointment_time=D(9, 0), arrival_time=D(8, 45), check_in_time=D(8, 55), release_time=D(12, 30))


def actual(session, tl=None, charges=None, bol="B1", evidence=None, ref="TRL-1", mid="M1", carrier="CARR-7"):
    ev = evidence if evidence is not None else {"tl": tl if tl is not None else TL}
    req = ActualRequest(movement_id=mid, bol_number=bol, carrier_id=carrier, container_or_job_id=ref,
                        claimed_charges=charges or [{"charge_code": "DET", "amount": 262.5}], evidence=ev)
    return Reconciler(session).actual(req)


def test1_tl_detention_example(session):
    r = actual(session)
    li = r.line_items[0]
    assert (li.amount_supported, li.variance) == (112.50, 150.00)
    assert li.recommended_action == A.PARTIAL_APPROVE and r.overall_action == A.PARTIAL_APPROVE


def test2_late_arrival_forfeits_protection():
    res = evaluate_tl_detention(TLDetentionInput(
        **{**TL, "arrival_time": D(11, 0), "check_in_time": D(11, 10), "dock_start_time": D(11, 30),
           "release_time": D(14, 30)}, start_rule="APPOINTMENT_IF_ON_TIME", free_time_minutes=120, hourly_rate=75, rounding_rule=RoundingRule.EXACT))
    assert res.protection_forfeited and res.clock_start == D(11, 30)
    assert res.billable_units == 60 and res.amount_supported == 75.0


def test3_rounding_modes():
    assert apply_rounding(35, RoundingRule.CEIL_15_MIN) == 45
    assert apply_rounding(35, RoundingRule.CEIL_30_MIN) == 60
    assert apply_rounding(35, RoundingRule.NEAREST_HOUR) == 60
    assert apply_rounding(20, RoundingRule.NEAREST_HOUR) == 0
    assert apply_rounding(35, RoundingRule.EXACT) == 35


def test4_ocean_demurrage_no_appointment_days(session):
    dem = dict(discharge_date=date(2025, 3, 1), available_date=date(2025, 3, 1), last_free_day=date(2025, 3, 4),
               gate_out_date=date(2025, 3, 7), documented_no_appointment_dates=[date(2025, 3, 5), date(2025, 3, 6)])
    clk = evaluate_ocean_demurrage(DemurrageInput(**dem, daily_rate_tiers=[
        RateTier(from_day=1, to_day=4, rate=100), RateTier(from_day=5, rate=250)]))
    assert clk.amount_disputed == 500.0
    r = actual(session, evidence={"demurrage": dem}, charges=[{"charge_code": "DEM", "amount": 750}], ref="MSKU1")
    main, review = r.line_items
    assert (main.amount_supported, main.recommended_action) == (250.0, A.APPROVE)
    assert (review.amount_claimed, review.variance, review.recommended_action) == (500.0, 500.0, A.HITL_REVIEW)
    assert review.reason_code == "APPOINTMENT_CONSTRAINED" and "dispute" in review.findings


def test5_missing_evidence_not_guessed(session):
    r = actual(session, tl={**TL, "check_in_time": None, "app_arrival_time": D(9, 5)},
               charges=[{"charge_code": "DET", "amount": 150}])
    assert r.overall_action == A.REQUEST_MISSING_PROOF and r.line_items[0].amount_supported == 0.0


def test6_duplicate_blocked(session):
    first = actual(session, charges=[{"charge_code": "DET", "amount": 112.5}], mid="M6a")
    assert first.overall_action == A.APPROVE
    second = actual(session, charges=[{"charge_code": "DET", "amount": 112.5}], mid="M6b")
    assert second.total_supported == 0.0 and any("DUPLICATE_CLAIM" in a for a in second.fraud_alerts)


def test7_service_track(session):
    res = evaluate_service(ServiceInput(job_id="J", technician_check_in=D(8, 0), customer_otp_sign_off=D(13, 30),
                                        base_package_hours=4, base_package_fee=800, overtime_hourly_rate=100))
    assert res.amount_supported == 950.0
    r = actual(session, evidence={"service": {"job_id": "J", "technician_check_in": D(8, 0),
                                              "customer_otp_sign_off": D(13, 30)}},
               charges=[{"charge_code": "SVC", "amount": 950}], ref="J")
    assert r.overall_action == A.APPROVE


def test_ghost_fee_blocked_unless_receipted(session):
    r = actual(session, charges=[{"charge_code": "LFT", "amount": 50}], evidence={})
    assert any("UNAUTHORIZED_GHOST_FEE" in a for a in r.fraud_alerts) and r.total_supported == 0
    ok = actual(session, charges=[{"charge_code": "LFT", "amount": 50, "receipt_ref": "RCPT-1"}], bol="B2", evidence={})
    assert not ok.fraud_alerts


def test_fuel_peg_from_db_index(session):
    r = actual(session, evidence={"fuel": {"ship_date": date(2025, 3, 12), "miles": 100}},
               charges=[{"charge_code": "FSC", "amount": 60.0}])
    assert r.line_items[0].amount_supported == 40.8 and any("FUEL_PEG_VIOLATION" in a for a in r.fraud_alerts)


def test_overlapping_clock(session):
    r = actual(session, evidence={"tl": TL, "time_claims": [
        {"charge_code": "DET", "start": D(9, 0), "end": D(13, 0)},
        {"charge_code": "LAYOVER", "start": D(10, 0), "end": D(14, 0)}]},
        charges=[{"charge_code": "DET", "amount": 300}])
    assert any("OVERLAPPING_CLOCK" in a for a in r.fraud_alerts) and r.total_supported == 0


def test_no_contract_means_missing_terms(session):
    r = actual(session, carrier="UNKNOWN", charges=[{"charge_code": "DEM", "amount": 10}],
               evidence={"demurrage": {"discharge_date": date(2025, 3, 1), "available_date": date(2025, 3, 1),
                                       "last_free_day": date(2025, 3, 4), "gate_out_date": date(2025, 3, 7)}})
    assert r.overall_action == A.HITL_REVIEW and r.line_items[0].reason_code == "MISSING_CONTRACT"


def test_api_flow_and_memo(client):
    prov = client.post("/api/v1/reconcile/provisional", json={
        "movement_id": "A1", "bol_number": "B", "carrier_id": "CARR-7", "origin": "X", "destination": "Y",
        "origin_departure": "2025-03-10T06:00:00", "miles": 100})
    assert prov.status_code == 200 and prov.json()["total_claimed"] == 250 + 40.8
    body = {"movement_id": "A1", "bol_number": "B", "carrier_id": "CARR-7", "container_or_job_id": "T",
            "claimed_charges": [{"charge_code": "DET", "amount": 262.5}, {"charge_code": "LH", "amount": 250}],
            "evidence": {"tl": {k: v.isoformat() for k, v in TL.items()}}}
    act = client.post("/api/v1/reconcile/actual", json=body).json()
    assert act["total_supported"] == 362.5 and act["provisional_total"] == 290.8 and act["audit_proof_pack_id"].startswith("APP-")
    memo = client.post("/api/v1/reconcile/dispute-memo", json={"movement_id": "A1"})
    assert memo.status_code == 200 and memo.content.startswith(b"%PDF")
    body["movement_id"] = "A2"
    client.post("/api/v1/reconcile/actual", json=body)
    alerts = client.get("/api/v1/reconcile/fraud-alerts").json()
    assert alerts[0]["alert_type"] == "DUPLICATE_CLAIM"
    assert client.post("/api/v1/reconcile/dispute-memo", json={"movement_id": "nope"}).status_code == 404


def test_scenarios_are_data_driven(client):
    sc = client.get("/api/v1/reconcile/scenarios").json()
    assert [s["name"] for s in sc][:4] == ["A", "B", "C", "D"] and "{{DATE" not in str(sc)


def test_auth_enforced(client, monkeypatch):
    from vectura.config import get_settings
    cfg = get_settings()
    monkeypatch.setattr(cfg, "auth_enabled", True)
    monkeypatch.setattr(cfg, "api_keys", {"secret": {"user": "ann", "role": "analyst"}})
    assert client.get("/api/v1/reconcile/fraud-alerts").status_code == 401
    assert client.get("/api/v1/reconcile/fraud-alerts", headers={"X-API-Key": "secret"}).status_code == 200
