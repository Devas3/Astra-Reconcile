"""U.S. DOE/EIA weekly diesel index: DB-backed lookups plus a sync from the free EIA Open Data API.

Run `python -m vectura.eia_fuel_index` (cron / Windows Task Scheduler weekly) to refresh the table.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import FuelIndexWeek

log = logging.getLogger(__name__)


def prevailing_monday(on: date) -> date:
    """Monday of the week containing `on`."""
    return on - timedelta(days=on.weekday())


def diesel_price(session: Session, on: date) -> Optional[float]:
    """Latest known price for the week containing `on` (row at or before that Monday), else None."""
    row = (session.query(FuelIndexWeek)
           .filter(FuelIndexWeek.week_start <= prevailing_monday(on))
           .order_by(FuelIndexWeek.week_start.desc()).first())
    return row.price if row else None


def fsc_per_mile(price: float, base_price: float, mpg: float, decimals: int) -> float:
    """Surcharge per mile = max(0, price - base) / mpg."""
    return round(max(0.0, price - base_price) / mpg, decimals)


def sync_from_eia(session: Session, settings: Optional[Settings] = None) -> int:
    """Upsert recent weekly prices from the EIA API. Returns rows written."""
    st = settings or get_settings()
    if not st.eia_api_key:
        raise RuntimeError("VECTURA_EIA_API_KEY is not set (free key: https://www.eia.gov/opendata/)")
    params = {
        "api_key": st.eia_api_key, "frequency": "weekly", "data[0]": "value",
        "facets[series][]": st.eia_series, "sort[0][column]": "period", "sort[0][direction]": "desc",
        "length": st.eia_history_weeks,
        "start": (date.today() - timedelta(weeks=st.eia_history_weeks)).isoformat(),  # keeps the query small and fast
    }
    resp = httpx.get(st.eia_base_url, params=params, timeout=st.eia_timeout_s)
    resp.raise_for_status()
    rows = resp.json()["response"]["data"]
    n = 0
    for r in rows:
        if r.get("value") in (None, ""):
            continue
        wk = date.fromisoformat(r["period"])
        existing = session.get(FuelIndexWeek, wk)
        if existing:
            existing.price, existing.source = float(r["value"]), "EIA_API"
        else:
            session.add(FuelIndexWeek(week_start=wk, price=float(r["value"]), source="EIA_API"))
        n += 1
    session.commit()
    log.info("eia_sync_complete", extra={"rows": n})
    return n


def main() -> int:
    """CLI used by run.ps1 / a weekly scheduler. Never crashes the caller: prints why a sync was skipped."""
    from sqlalchemy.orm import sessionmaker

    from .db import get_engine
    try:
        with sessionmaker(get_engine())() as s:
            print(f"EIA sync: {sync_from_eia(s)} weekly prices saved")
        return 0
    except RuntimeError as exc:  # no key configured
        print(f"EIA sync skipped: {exc}")
    except httpx.HTTPError as exc:
        print(f"EIA sync failed ({type(exc).__name__}): existing prices are kept. {exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
