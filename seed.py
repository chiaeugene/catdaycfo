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
# to exactly these 3 accounts: it's the source of truth for who can get in.
# username, passcode, display_name, role
USERS = [
    ("jasmine", "125180", "Jasmine", "viewer"),     # the boss — sees everything, changes nothing
    ("eugene", "455223", "Eugene", "admin"),
    ("wengteng", "290226", "Weng Teng", "admin"),
]
allowed_usernames = {u for u, *_ in USERS}
for username, passcode, name, role in USERS:
    u = db.query(User).filter(User.username == username).first()
    if u:
        u.password_hash, u.display_name, u.role, u.active = hash_password(passcode), name, role, True
        print(f"User updated: {username} ({role})")
    else:
        db.add(User(username=username, password_hash=hash_password(passcode),
                    display_name=name, role=role, active=True))
        print(f"User created: {username} ({role})")

# Only these 3 accounts should ever be able to log in — deactivate anything else.
db.flush()
for u in db.query(User).all():
    if u.username not in allowed_usernames and u.active:
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
db.close()
print("Seed complete.")
