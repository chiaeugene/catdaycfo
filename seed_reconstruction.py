"""Go-live cutover: wipe all sample/demo data, load Karen's real forensic
reconstruction ("CatDay & Meow Me - Financial Summary and Accounting
Reconstruction (31 Jul 2026)", delivered 12 Aug 2026) as the system's opening
position. Run AFTER seed.py:  python seed_reconstruction.py

WHAT THIS DOES
- Deletes every sample/demo record (documents, payments, vouchers, sales,
  petty cash, staff, suppliers, bank data, payroll, journal — the full
  seed_sample.py universe) so nothing fictional survives into production.
- Adds 4 new Chart of Accounts lines the reconstruction needs (see below).
- Loads the real suppliers and staff identified in the reconstruction.
- Loads a real CIMB bank account (86-0637162-5) with its 31 Jul balance.
- Posts ONE opening-balance journal entry that exactly mirrors Karen's own
  "Balance Sheet Opening" tab — the tab she titled "What the Company
  Currently Has and Owes".

WHAT THIS DELIBERATELY DOES NOT DO — and why
Karen's own workbook is explicit that several figures are NOT ready to post:
  - RM152,406.84 of her claims: "Amount posted to reconstructed books: 0.00
    — Await claim approval, reimbursement ledger and duplicate review."
  - CNY187,000 + CNY49,800 of China-sourced assets: "Do not post until MYR
    debit/FX is supplied."
  - The RM220,000 CDM deposits and RM785,871.60 of off-bank CAPEX payments
    are real money that moved, but their SOURCE (equity vs director loan vs
    something else) is unconfirmed — so they're booked as liabilities in a
    clearly labeled suspense account, never as capital, until that question
    (Q01/CQ-04 in her Questions Outstanding tab) is answered.
This script follows her own conclusions rather than silently reinterpreting
them. When those questions get answered, correct the opening entry (or add a
follow-up manual journal entry) rather than editing this file's numbers.

Idempotent: guarded by RECONSTRUCTION_DATA_VERSION, safe to re-run/re-deploy.
"""
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from app.database import Base, engine, SessionLocal, run_migrations
from app import models as M
from app import ledger
from app.statutory import calc_statutory

Base.metadata.create_all(engine)
run_migrations()
db = SessionLocal()

RECON_VERSION = "v1-31jul2026"
ver_setting = db.get(M.Setting, "RECONSTRUCTION_DATA_VERSION")
if ver_setting and ver_setting.value == RECON_VERSION:
    print("Reconstruction data already loaded (current version) - skipping.")
    db.close()
    raise SystemExit(0)

# ── Wipe every sample/demo record ────────────────────────────────────────────
for model in (M.PayrollItem, M.PayrollRun, M.StatutoryPaid, M.PettyCashEntry,
              M.PettyCashAccount, M.SalesEntry, M.BoardingLog, M.Voucher, M.Listing,
              M.Document, M.Payment, M.Supplier, M.Staff, M.BankStatementLine,
              M.BankAccount, M.JournalLine, M.JournalEntry):
    db.query(model).delete()
for name in ("DOC", "PAY", "PV", "PL", "MJE"):
    c = db.get(M.Counter, name)
    if c:
        c.value = 1
db.commit()
print("Wiped sample/demo data.")

# ── Chart of Accounts: 4 new accounts the reconstruction needs ──────────────
ledger.seed_coa(db)
NEW_ACCOUNTS = [
    ("1015", "Cash & TNG Clearing (Unverified)", "Asset"),
    ("1620", "Fixed Assets — Provisional Candidates", "Asset"),
    ("2900", "Unclassified Funding Suspense", "Liability"),
    ("2910", "Off-Bank CAPEX Funding Suspense", "Liability"),
]
existing = {a.code for a in db.query(M.Account).all()}
for code, name, typ in NEW_ACCOUNTS:
    if code not in existing:
        db.add(M.Account(code=code, name=name, type=typ))
db.commit()
print(f"Chart of Accounts: added {sum(1 for c,_,_ in NEW_ACCOUNTS if c not in existing)} new accounts.")

# ── Real suppliers identified in the reconstruction ──────────────────────────
# Bank details are not in the source evidence except where noted — left blank
# rather than guessed. China suppliers are excluded: no registered legal name
# or bank detail was supplied, only descriptive text (see CAPEX Fixed Assets
# tab CAP-006/007/008/009) — adding them as formal records would fabricate
# certainty that isn't in the evidence.
SUPPLIERS = [
    ("TNJ Builders Sdn Bhd", "Contractor",
     "Main renovation contractor. RM890,853.10 agreed contract, RM776,975.30 "
     "invoiced, RM776,971.60 payments acknowledged, RM3.70 balance per their "
     "31-Aug-2026 supplier statement. Payer-side proof still outstanding (CQ-01)."),
    ("Flow Elite Engineering Sdn Bhd", "Contractor",
     "Mechanical ventilation installation. RM13,600 invoiced, RM8,900 paid, "
     "RM4,700 remaining unconfirmed (CQ-08)."),
    ("Alwayz (M) Sdn Bhd", "Supplier",
     "Membrane ceiling installation. RM9,761.75 paid; final tax invoice and "
     "credit note still outstanding (CQ-09)."),
    ("Big Tree Resources Sdn Bhd", "Service Provider",
     "Freight/logistics for China-sourced cat houses. RM2,638.95 paid; "
     "RM32,137.50 of invoices are addressed to R.S Auto Accessories, not "
     "Meow & Me — reissued invoices or cross-charge trail needed."),
    ("Great Eastern", "Service Provider", "Construction all-risk / workmen compensation insurance, RM519.27."),
    ("Winston Bedding Global Sdn Bhd", "Supplier",
     "7 air-conditioning units, RM6,000 invoiced to Meow & Me; payment unverified."),
    ("QQT Express Sdn Bhd", "Service Provider", "Freight for CatDay furniture shipment, RM5,204.98 invoiced; payment unverified."),
    ("Majlis Bandaraya Petaling Jaya (MBPJ)", "Utility", "Licences/permits, RM1,450 paid."),
    ("Medklinn International", "Supplier", "Air/surface sterilisers, RM7,196.80 paid; CAPEX vs expense treatment pending."),
    ("Seo Aik Leong", "Landlord", "Landlord — monthly rent RM17,000 per CIMB narration. Signed lease not yet supplied (Q08)."),
]
for name, sup_type, notes in SUPPLIERS:
    if not db.query(M.Supplier).filter(M.Supplier.name == name).first():
        db.add(M.Supplier(name=name, sup_type=sup_type, notes=notes))
db.commit()
print(f"Loaded {len(SUPPLIERS)} real suppliers.")

# ── Real staff identified in bank payroll narrations ─────────────────────────
# Reconstruction Q09 explicitly asks whether "NG SOCK HWA @ SUI SOCK HWA" is
# Karen herself — unresolved. Loaded as-is, position marked unconfirmed, salary
# = last confirmed monthly payment. Do not assume role/identity beyond this.
STAFF = [
    ("Nur Liyana Binti Rosli", "Unconfirmed — pending role confirmation", 2627.20),
    ("Ng Sock Hwa @ Sui Sock Hwa", "Unconfirmed — pending role confirmation (see Q09)", 6610.95),
]
for name, pos, base in STAFF:
    st = calc_statutory(base)
    db.add(M.Staff(name=name, position=pos, base_salary=base, allowance=0,
                   epf_employer=st["epf_er"], epf_employee=st["epf_ee"],
                   socso_employer=st["socso_er"], socso_employee=st["socso_ee"],
                   eis_employer=st["eis_er"], eis_employee=st["eis_ee"]))
db.commit()
print(f"Loaded {len(STAFF)} real staff (identity/role pending confirmation).")

# ── Real bank account ─────────────────────────────────────────────────────────
db.add(M.BankAccount(name="CIMB Current (Meow & Me)", bank_name="CIMB Bank",
                     account_no="86-0637162-5", opening_balance=55941.09))
db.commit()
print("Loaded real CIMB bank account.")

# ── Opening balance journal entry — mirrors Karen's "Balance Sheet Opening" tab exactly ──
accs = {a.code: a for a in db.query(M.Account).all()}
OPENING_DATE = date(2026, 7, 31)

# (account code, amount, description) — assets and liabilities only; the
# difference plugs to 3900 Opening Balance Equity, same mechanism as the
# /accounting/coa opening-balance form.
LINES = [
    ("1020", 55941.09, "CIMB bank — 31 Jul closing balance (confirmed)"),
    ("1015", 33855.00, "Cash + TNG sales, inferred balance — no 31 Jul cash count (Q04/Q05)"),
    ("1620", 9835.75, "Medklinn + Big Tree freight — provisional asset candidates"),
    ("1600", 795633.35, "MYR renovation/equipment work in progress — TNJ, Flow, Alwayz; excludes insurance, Big Tree, all CNY assets"),
    ("2900", -220000.00, "RM220,000 CDM deposits — source unconfirmed, do not treat as capital until proven (Q01)"),
    ("2910", -785871.60, "CAPEX payments acknowledged by TNJ/Flow but not in supplied CIMB statements — payer unresolved (CQ-03/CQ-04)"),
]
total_dr = sum(a for _, a, _ in LINES if a > 0)
total_cr = sum(-a for _, a, _ in LINES if a < 0)
plug = round(total_dr - total_cr, 2)   # negative = accumulated provisional loss

lines = []
for code, amt, desc in LINES:
    aid = accs[code].id
    if amt >= 0:
        lines.append((aid, amt, 0, desc))
    else:
        lines.append((aid, 0, -amt, desc))
if abs(plug) > 0.005:
    obe = accs["3900"]
    if plug > 0:
        lines.append((obe.id, 0, plug, "Opening balance equity (plug)"))
    else:
        lines.append((obe.id, -plug, 0, "Opening balance equity — accumulated provisional loss (plug)"))

je = ledger.post_manual(
    db, OPENING_DATE,
    "Opening position at 31 Jul 2026 — from Karen's Financial Summary & "
    "Accounting Reconstruction (12 Aug 2026). WORKING STATUS: EVIDENCE "
    "PENDING, not statutory accounts. See Questions Outstanding tab (32 "
    "items) before treating any figure here as final.",
    lines, "Reconstruction import", source_type="Opening", ref="OPENING-31JUL26")
db.commit()

rows, tot_dr, tot_cr = ledger.trial_balance(db, OPENING_DATE)
bs = ledger.balance_sheet(db, OPENING_DATE)
print(f"Opening journal posted: {len(je.lines)} lines.")
print(f"Trial balance: Dr {tot_dr:,.2f}  Cr {tot_cr:,.2f}  balanced={abs(tot_dr-tot_cr)<0.01}")
print(f"Balance sheet: assets {bs['total_assets']:,.2f} = liab {bs['total_liabilities']:,.2f} + equity {bs['total_equity']:,.2f}  balanced={bs['balanced']}")

ver = db.get(M.Setting, "RECONSTRUCTION_DATA_VERSION")
if not ver:
    ver = M.Setting(key="RECONSTRUCTION_DATA_VERSION")
    db.add(ver)
ver.value = RECON_VERSION
db.commit()
db.close()
print("Reconstruction load complete.")
