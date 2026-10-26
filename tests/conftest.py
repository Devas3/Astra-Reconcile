import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vectura import db, models  # noqa: E402,F401
from vectura.seed import seed  # noqa: E402


@pytest.fixture()
def session():
    """Fresh in-memory DB per test, seeded from data/seed (the same files production uses)."""
    engine = db.make_engine("sqlite:///:memory:")
    db.Base.metadata.create_all(engine)
    db.configure(engine)
    with sessionmaker(engine, expire_on_commit=False)() as s:
        seed(s)
        yield s


@pytest.fixture()
def client(session):
    from fastapi.testclient import TestClient

    from vectura.api import app
    return TestClient(app)
