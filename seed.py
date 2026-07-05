"""One-time seed: admin users + the 9 CATDAY staff with Malaysian statutory presets.
Safe to re-run (skips existing). Values are editable presets, not tax advice —
verify against actual EPF/SOCSO/EIS tables.
"""
from dotenv import load_dotenv

load_dotenv()

from app.database import Base, engine, SessionLocal
from app.models import User, Staff, Setting
from app.auth import hash_password

Base.metadata.create_all(engine)
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

# name, position, basic, allowance, epf_er, epf_ee, socso_er, socso_ee, eis_er, eis_ee
STAFF = [
    ("Karen",             "Feline Care Director", 8000, 0, 960.00, 880.00, 104.15, 29.75, 9.90, 9.90),
    ("Cat Caretaker 1",   "Cat Caretaker",        1700, 0, 221.00, 187.00,  29.75,  8.50, 3.40, 3.40),
    ("Cat Caretaker 2",   "Cat Caretaker",        1700, 0, 221.00, 187.00,  29.75,  8.50, 3.40, 3.40),
    ("Chief Concierge",   "Reception",            2634, 0, 342.45, 289.75,  46.15, 13.25, 5.30, 5.30),
    ("Senior Groomer",    "Senior Groomer",       3040, 0, 395.20, 334.40,  53.15, 15.25, 6.10, 6.10),
    ("Junior Groomer",    "Junior Groomer",       1700, 0, 221.00, 187.00,  29.75,  8.50, 3.40, 3.40),
    ("Steward 1",         "Housekeeping",         1600, 0, 208.00, 176.00,  27.95,  8.05, 3.20, 3.20),
    ("Steward 2",         "Housekeeping",         1600, 0, 208.00, 176.00,  27.95,  8.05, 3.20, 3.20),
    ("Community Curator", "Community & Media",    3040, 0, 395.20, 334.40,  53.15, 15.25, 6.10, 6.10),
]
if db.query(Staff).count() == 0:
    for name, pos, base, allw, epf_er, epf_ee, soc_er, soc_ee, eis_er, eis_ee in STAFF:
        db.add(Staff(name=name, position=pos, base_salary=base, allowance=allw,
                     epf_employer=epf_er, epf_employee=epf_ee,
                     socso_employer=soc_er, socso_employee=soc_ee,
                     eis_employer=eis_er, eis_employee=eis_ee))
    print(f"Seeded {len(STAFF)} staff with statutory presets.")

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

db.commit()
db.close()
print("Seed complete.")
