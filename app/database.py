import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DB_PATH = os.environ.get("DATABASE_URL", "sqlite:///./catday.db")
engine = create_engine(DB_PATH, connect_args={"check_same_thread": False} if DB_PATH.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# Additive column migrations — create_all() never alters existing tables, so
# new columns on already-deployed databases must be added explicitly. Runs from
# both the seed scripts and app startup, before any code reads/writes them.
MIGRATIONS = [
    "ALTER TABLE payments ADD COLUMN invoice_no VARCHAR(60) DEFAULT ''",
    "ALTER TABLE documents ADD COLUMN invoice_no VARCHAR(60) DEFAULT ''",
    "ALTER TABLE documents ADD COLUMN intake_type VARCHAR(30) DEFAULT 'Document'",
    "ALTER TABLE documents ADD COLUMN payload_json TEXT DEFAULT ''",
    "ALTER TABLE documents ADD COLUMN raw_text TEXT DEFAULT ''",
    "ALTER TABLE payments ADD COLUMN tax_type VARCHAR(20) DEFAULT 'None'",
    "ALTER TABLE payments ADD COLUMN tax_amount FLOAT DEFAULT 0",
    "ALTER TABLE sales ADD COLUMN tax_type VARCHAR(20) DEFAULT 'None'",
    "ALTER TABLE sales ADD COLUMN tax_amount FLOAT DEFAULT 0",
    "ALTER TABLE payroll_items ADD COLUMN pcb FLOAT DEFAULT 0",
]


def run_migrations():
    from sqlalchemy import text
    with engine.connect() as conn:
        for stmt in MIGRATIONS:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()  # column already exists — fine


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
