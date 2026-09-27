"""One-time seed: admin user, base Settings, and the Chart of Accounts.
Safe to re-run (skips existing). Staff/suppliers/opening balances are real
business data and live in seed_reconstruction.py, not here.
"""
from dotenv import load_dotenv

load_dotenv()

from app.database import Base, engine, SessionLocal, run_migrations
from app.models import User, Setting
from app.auth import hash_password

# Snapshot BEFORE any schema change or seeding touches the data. This runs
# first in the deploy chain, so if a migration or seed script goes wrong there
# is always a restore point from the moment before it ran.
try:
    from app.backup import make_snapshot
    _snap = make_snapshot("predeploy")
    if _snap:
        print(f"Pre-deploy backup: {_snap['name']} ({_snap['size']:,} bytes)")
except Exception as e:      # never block a deploy on a backup failure
    print(f"Pre-deploy backup skipped: {e}")

Base.metadata.create_all(engine)
run_migrations()
db = SessionLocal()

# Each person's passcode identifies them at login — no shared system passcode
# and no profile picker. Re-running this script always converges access back
# to exactly these accounts: it's the source of truth for who can get in.
# username, passcode, display_name, role
USERS = [
    ("jasmine", "125180", "Jasmine", "viewer"),     # the boss — sees everything, changes nothing
    ("eugene", "455223", "Eugene", "admin"),
    ("wengteng", "290226", "Weng Teng", "admin"),
    # Storekeeper: issues customer invoices, raises purchase requests, stock.
    # Cannot record payments or see the books (enforced in app/audit.py).
    ("karen", "641663", "Karen", "storekeeper"),
]
allowed_usernames = {u for u, *_ in USERS}
for username, passcode, name, role in USERS:
    u = db.query(User).filter(User.username == username).first()
    if u:
        u.password_hash, u.display_name, u.role, u.active = hash_password(passcode), name, role, True
        u.can_approve = role != "storekeeper"   # Jasmine, Eugene, Weng Teng approve
        print(f"User updated: {username} ({role})")
    else:
        db.add(User(username=username, password_hash=hash_password(passcode),
                    display_name=name, role=role, active=True,
                    can_approve=role != "storekeeper"))
        print(f"User created: {username} ({role})")

# Only these accounts get into the books. Requester accounts (the shop
# operator raising purchase requests) are made in Settings and must survive a
# redeploy -- they can't see the books, so they're outside this rule.
db.flush()
for u in db.query(User).all():
    if u.username not in allowed_usernames and u.active and u.role != "requester":
        u.active = False
        print(f"User deactivated (not in allowed list): {u.username}")

DEFAULTS = {
    # Legal entity behind the cat day brand. Documents show the cat day logo
    # but must carry the registered name and company number.
    "COMPANY_NAME": "MEOW & ME PET SHOP SDN BHD",
    "COMPANY_ADDRESS": "No 34, Jalan SS21/1, Damansara Utama, 47400 Petaling Jaya, Selangor",
    "COMPANY_ROC": "202501052347",
    "TELEGRAM_WHITELIST": "*",
    "PETTY_CASH_FLOAT": "5000",
}
for k, v in DEFAULTS.items():
    if not db.get(Setting, k):
        db.add(Setting(key=k, value=v))

# Chart of Accounts for the double-entry ledger (idempotent)
from app.ledger import seed_coa
seed_coa(db)

db.commit()

# One-time data fixes, each marked done so it runs once. Never block a deploy.
try:
    from app.backfills import run_all
    run_all(db)
except Exception as e:
    db.rollback()
    print(f"Backfill skipped: {e}")

db.close()
print("Seed complete.")
