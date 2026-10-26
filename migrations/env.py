from alembic import context
from vectura import models  # noqa: F401  (register tables)
from vectura.db import Base, make_engine

target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = make_engine()  # URL from VECTURA_DATABASE_URL
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
