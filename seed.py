"""One-time seed: admin user, base Settings, and the Chart of Accounts.
Safe to re-run (skips existing). Staff/suppliers/opening balances are real
business data and live in seed_reconstruction.py, not here.
"""
from dotenv import load_dotenv

load_dotenv()

from app.database import Base, engine, SessionLocal, run_migrations
from app.models import User, Setting
from app.auth import hash_password

Base.metadata.create_all(engine)
run_migrations()
db = SessionLocal()

USERS = [
    ("jasmine", "catday2026", "Jasmine", "admin"),
]
for username, pw, name, role in USERS:
    if not db.query(User).filter(User.username == username).first():
        db.add(User(username=username, password_hash=hash_password(pw),
                    display_name=name, role=role))
        print(f"User created: {username}  ({role})")

# Single-identity migration: only Jasmine stays active; normalize any
# old names in existing records so one name appears system-wide.
db.flush()
for u in db.query(User).all():
    u.active = u.username == "jasmine"
OLD_NAMES = ("Eugene", "Karen", "Jason", "Aina")
from app.models import Document, Payment, Voucher, Listing, PettyCashEntry, SalesEntry
for model, fields in [
    (Document, ("sender", "verified_by")),
    (Voucher, ("created_by", "approved_by")),
    (Listing, ("prepared_by",)),
    (PettyCashEntry, ("recorded_by",)),
    (SalesEntry, ("recorded_by",)),
]:
    for row in db.query(model).all():
        for f in fields:
            if getattr(row, f) in OLD_NAMES:
                setattr(row, f, "Jasmine")

DEFAULTS = {
    "COMPANY_NAME": "CATDAY SDN BHD",
    "COMPANY_ADDRESS": "Uptown PJ, Petaling Jaya",
    "TELEGRAM_WHITELIST": "*",
    "PETTY_CASH_FLOAT": "5000",
    "PASSCODE": "125180",
}
for k, v in DEFAULTS.items():
    if not db.get(Setting, k):
        db.add(Setting(key=k, value=v))

# Chart of Accounts for the double-entry ledger (idempotent)
from app.ledger import seed_coa
seed_coa(db)

db.commit()
db.close()
print("Seed complete.")
