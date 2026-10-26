"""Backend features the dashboard relies on: dry-run previews and event-type lookup."""
API = "/api/v1/reconcile"
BODY = {"movement_id": "UI1", "bol_number": "UIB", "carrier_id": "CARR-7", "container_or_job_id": "T",
        "claimed_charges": [{"charge_code": "DET", "amount": 262.5}],
        "evidence": {"tl": {"appointment_time": "2025-03-10T09:00:00", "arrival_time": "2025-03-10T08:45:00",
                            "check_in_time": "2025-03-10T08:55:00", "release_time": "2025-03-10T12:30:00"}}}


def test_dry_run_returns_full_result_but_saves_nothing(client):
    r = client.post(f"{API}/actual", json={**BODY, "dry_run": True}).json()
    assert r["total_supported"] == 112.5 and r["line_items"][0]["variance"] == 150
    assert client.get(f"{API}/cases").json() == []
    assert client.get(f"{API}/fraud-alerts").json() == []
    # because nothing was recorded, the real submission is not a duplicate
    real = client.post(f"{API}/actual", json=BODY).json()
    assert real["fraud_alerts"] == [] and len(client.get(f"{API}/cases").json()) == 1


def test_dry_run_does_not_hide_a_real_duplicate(client):
    client.post(f"{API}/actual", json=BODY)
    again = client.post(f"{API}/actual", json={**BODY, "movement_id": "UI2", "dry_run": True}).json()
    assert any("DUPLICATE_CLAIM" in a for a in again["fraud_alerts"])
    assert len(client.get(f"{API}/cases").json()) == 1  # preview wrote no second case


def test_event_types_list_sources_by_rank(client):
    types = {t["code"]: t for t in client.get(f"{API}/event-types").json()}
    assert types["RELEASE"]["closes_clock"] is True
    assert [s["source"] for s in types["ARRIVAL"]["sources"]] == ["GATE", "GPS"]
