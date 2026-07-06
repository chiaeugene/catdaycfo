"""Sample data: 10 days of CATDAY operations (26 Jun – 5 Jul 2026).

Creates real PDF document files (as if received via Telegram), verified and
pending documents, payments in every lifecycle stage, vouchers + a listing
with generated PDFs, petty cash book, daily sales, and a confirmed June
payroll run. Run AFTER seed.py:  python seed_sample.py

Role convention: Jasmine is the master admin — she only appears on admin
actions (verify / approve / prepare / create). Everyday submissions (sending
a document via Telegram, recording a sale, logging petty cash) are attributed
to front-line role labels (Front Desk, Groomer, Ops Team, Cat Care), since
those are done by whoever is on shift, not the admin herself.
"""
import os
import random
from datetime import date, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from reportlab.lib.pagesizes import A6
from reportlab.pdfgen import canvas

from app.database import Base, engine, SessionLocal
from app import models as M
from app import pdfgen

random.seed(42)
Base.metadata.create_all(engine)
db = SessionLocal()

SAMPLE_VERSION = "v4-suppliers"
ver_setting = db.get(M.Setting, "SAMPLE_DATA_VERSION")

if ver_setting and ver_setting.value == SAMPLE_VERSION:
    print("Sample data already present (current version) - skipping.")
    db.close()
    raise SystemExit(0)

# Wipe any previous sample data (from an older version of this script) before reseeding.
if db.query(M.SalesEntry).count() > 0 or db.query(M.Document).count() > 0:
    for model in (M.PayrollItem, M.PayrollRun, M.PettyCashEntry, M.SalesEntry,
                  M.Voucher, M.Listing, M.Document, M.Payment, M.Supplier):
        db.query(model).delete()
    for name in ("DOC", "PAY", "PV", "PL"):
        c = db.get(M.Counter, name)
        if c:
            c.value = 1
    db.commit()
    print("Cleared previous sample data for reseed.")

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
D0 = date(2026, 6, 26)          # first day of operations
DAYS = [D0 + timedelta(days=i) for i in range(10)]   # 26 Jun – 5 Jul

ADMIN = "Jasmine"                 # master admin — verify/approve/prepare/create only
ROLES = ["Front Desk", "Groomer", "Ops Team", "Cat Care"]   # front-line submitters


def mstr(d): return f"{d:%b %Y}"


def counter(name, prefix):
    c = db.get(M.Counter, name)
    if not c:
        c = M.Counter(name=name, value=1)
        db.add(c)
        db.flush()
    n = c.value
    c.value += 1
    return f"{prefix}{n:04d}"


def make_doc_pdf(title, lines, d):
    """Create a small receipt-style PDF acting as the Telegram-received file."""
    subdir = f"{d:%Y-%m}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    doc_no = counter("DOC", "DOC-")
    rel = f"{subdir}/{doc_no}_{pdfgen.safe_name(title)}.pdf"
    c = canvas.Canvas(os.path.join(UPLOAD_DIR, rel), pagesize=A6)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20, 380, title)
    c.setFont("Helvetica", 9)
    y = 355
    for ln in lines:
        c.drawString(20, y, ln)
        y -= 16
    c.save()
    return doc_no, rel


# ═══ 0. Supplier / contractor directory (mock bank accounts) ═══
SUPPLIERS = [
    # name, type, bank, account_no, account_holder, contact, phone
    ("Uptown Realty",      "Landlord",         "Maybank",         "5142 8890 1234", "Uptown Realty Sdn Bhd",        "Mr. Tan",     "03-7725 1180"),
    ("TNJ Design",         "Contractor",       "CIMB Bank",       "8600 445 512",   "TNJ Design & Build Sdn Bhd",   "Jason Ng",    "012-336 8821"),
    ("Whiskers Wholesale", "Supplier",         "Public Bank",     "3159 002 214",   "Whiskers Wholesale Trading",   "Ms. Lim",     "03-8066 4432"),
    ("Litter King",        "Supplier",         "RHB Bank",        "2141 3300 5678", "Litter King Enterprise",       "Encik Farid", "017-772 0091"),
    ("GroomMaster MY",     "Supplier",         "Hong Leong Bank", "0091 5522 7834", "GroomMaster Malaysia Sdn Bhd", "Kevin Choo",  "016-889 2205"),
    ("CleanPro Supplies",  "Supplier",         "Maybank",         "5144 2216 8890", "CleanPro Supplies Sdn Bhd",    "Ms. Aisyah",  "03-5510 6672"),
    ("Meow Media",         "Service Provider", "CIMB Bank",       "8004 112 987",   "Meow Media Studio",            "Sarah Wong",  "018-224 5561"),
    ("SoftInv Systems",    "Service Provider", "OCBC Bank",       "790 441 2205",   "SoftInv Systems Sdn Bhd",      "Daniel Lee",  "03-2261 8845"),
    ("Klinik Haiwan PJ",   "Service Provider", "Public Bank",     "3160 448 872",   "Klinik Haiwan PJ Sdn Bhd",     "Dr. Priya",   "03-7960 3312"),
    ("TNB",                "Utility",          "",                "",               "",                              "",            "1-300-88-5454"),
]
SUP_BY_NAME = {}
for name, styp, bank, acct, holder, contact, phone in SUPPLIERS:
    s = M.Supplier(name=name, sup_type=styp, bank_name=bank, account_no=acct,
                   account_holder=holder, contact_person=contact, phone=phone)
    db.add(s)
    SUP_BY_NAME[name.lower()] = s
db.flush()


def bank_dict(payee):
    s = SUP_BY_NAME.get(payee.strip().lower())
    if s and (s.bank_name or s.account_no):
        return {"bank_name": s.bank_name, "account_no": s.account_no,
                "account_holder": s.account_holder}
    return None


def bank_line(payee):
    s = SUP_BY_NAME.get(payee.strip().lower())
    return f"{s.bank_name} {s.account_no}" if s and (s.bank_name or s.account_no) else ""


# ═══ 1. Documents + payments (verified pipeline) ═══
# (day_offset, sender_role, supplier, description, amount, category, grp, doc_title)
PURCHASES = [
    (0, "Ops Team",   "Uptown Realty",      "Rental July 2026",                17000.00, "Rental",            "OPEX",  "RENTAL INVOICE JUL-2026"),
    (0, "Cat Care",   "Whiskers Wholesale", "Cat food bulk order 40 x 2kg",     1860.00, "Cat Supplies",      "COGS",  "INVOICE WW-3311"),
    (1, "Ops Team",   "CleanPro Supplies",  "Cleaning chemicals + mop set",      412.60, "Maintenance",       "OPEX",  "RECEIPT CP-99213"),
    (2, "Front Desk", "TNB",                "Electricity deposit adjustment",   1230.00, "Utilities",         "OPEX",  "TNB STATEMENT 06/26"),
    (3, "Ops Team",   "TNJ Design",         "Renovation final touch-up works", 18500.00, "Renovation",        "CAPEX", "TNJ CLAIM #6"),
    (4, "Groomer",    "GroomMaster MY",     "Shampoo, dryers consumables",       684.30, "Grooming Supplies", "COGS",  "INVOICE GM-2207"),
    (5, "Front Desk", "Meow Media",         "Grand opening campaign boost",     3500.00, "Marketing",         "OPEX",  "INVOICE MM-0088"),
    (6, "Ops Team",   "SoftInv Systems",    "Booking system subscription",       299.00, "Software",          "OPEX",  "INVOICE SI-77120"),
    (7, "Cat Care",   "Litter King",        "Premium litter 30 bags",            945.00, "Cat Supplies",      "COGS",  "INVOICE LK-5501"),
    (8, "Cat Care",   "Klinik Haiwan PJ",   "New cat health screening x4",       520.00, "Vet",               "OPEX",  "RECEIPT KH-1904"),
]

payments = []
for off, sender, supplier, desc, amt, cat, grp, title in PURCHASES:
    d = DAYS[off]
    doc_no, rel = make_doc_pdf(title, [f"Supplier: {supplier}", f"Date: {d:%d/%m/%Y}",
                                       f"Description: {desc}", f"TOTAL: RM {amt:,.2f}"], d)
    pay_no = counter("PAY", "PAY-")
    p = M.Payment(pay_no=pay_no, date=d, supplier=supplier, description=desc,
                  category=cat, grp=grp, amount=amt, month=mstr(d),
                  status="Categorized", notes=f"from {doc_no} ({sender})")
    db.add(p)
    db.flush()
    doc = M.Document(doc_no=doc_no, received_at=datetime.combine(d, datetime.min.time()) + timedelta(hours=random.randint(9, 18)),
                     sender=sender, section="Purchase" if grp == "CAPEX" else "Expense",
                     doc_type="Invoice", supplier=supplier, amount=amt, month=mstr(d),
                     description=desc, category=cat, file_path=rel, mime="application/pdf",
                     status="Verified", ai_classified=True, verified_by=ADMIN,
                     verified_at=datetime.combine(d, datetime.min.time()) + timedelta(hours=20),
                     payment_id=p.id)
    db.add(doc)
    payments.append(p)

# 2 documents still pending verification (today's inbox)
for sender, desc, amt, title in [
    ("Ops Team", "Aircon servicing 4 units", 760.00, "QUOTE ACS-2288"),
    ("Front Desk", "Cat trees x3 for lobby", 1240.00, "INVOICE PETDECO-41"),
]:
    d = DAYS[-1]
    doc_no, rel = make_doc_pdf(title, [f"Date: {d:%d/%m/%Y}", f"Description: {desc}",
                                       f"TOTAL: RM {amt:,.2f}"], d)
    db.add(M.Document(doc_no=doc_no, received_at=datetime.now() - timedelta(hours=random.randint(1, 5)),
                      sender=sender, section="Expense", doc_type="Invoice", supplier="",
                      amount=amt, month=mstr(d), description=desc, category="",
                      file_path=rel, mime="application/pdf", status="Pending", ai_classified=True))

db.flush()

# ═══ 2. Vouchers + listing (admin actions — Jasmine) ═══
settings = {s.key: s.value for s in db.query(M.Setting).all()}
company = settings.get("COMPANY_NAME", "CATDAY SDN BHD")
address = settings.get("COMPANY_ADDRESS", "Uptown PJ")


def build_voucher(pays, payee, status, created=ADMIN, approved=""):
    pv_no = counter("PV", "PV-")
    total = sum(p.amount for p in pays)
    items = [{"date": f"{p.date:%d/%m/%y}", "description": p.description, "amount": p.amount} for p in pays]
    rel = pdfgen.voucher_pdf(pv_no, payee, items, total, company, address,
                             bank=bank_dict(payee))
    v = M.Voucher(pv_no=pv_no, date=pays[-1].date, payee=payee, total=total,
                  status=status, pdf_path=rel, created_by=created, approved_by=approved)
    db.add(v)
    db.flush()
    for p in pays:
        p.voucher_id = v.id
        p.status = "Paid" if status == "Paid" else "On Voucher"
    return v


v1 = build_voucher([payments[0]], "Uptown Realty", "Paid", approved=ADMIN)        # rental
v2 = build_voucher([payments[1], payments[6]], "Whiskers Wholesale", "Approved", approved=ADMIN)
v3 = build_voucher([payments[4]], "TNJ Design", "Draft")                              # renovation claim

# Listing containing the paid + approved vouchers
pl_no = counter("PL", "PL-")
vdata = [{"pv_no": v.pv_no, "date": f"{v.date:%d/%m/%y}", "payee": v.payee,
          "total": v.total, "bank": bank_line(v.payee)}
         for v in (v1, v2)]
rel = pdfgen.listing_pdf(pl_no, vdata, v1.total + v2.total, company, address)
pl = M.Listing(pl_no=pl_no, date=DAYS[5], total=v1.total + v2.total, status="Submitted",
               pdf_path=rel, prepared_by=ADMIN)
db.add(pl)
db.flush()
v1.listing_id = pl.id
v2.listing_id = pl.id

# ═══ 3. Petty cash (front-line recording; opening float is an admin action) ═══
db.add(M.PettyCashEntry(date=DAYS[0], description="Opening float", amount_in=5000,
                        month=mstr(DAYS[0]), recorded_by=ADMIN))
PC = [
    (1, "Parking + toll for supplier run", "Transport", 24.50, "Ops Team"),
    (2, "Emergency cat treats (fussy guest)", "Cat Supplies", 48.90, "Cat Care"),
    (3, "Staff lunch — opening week", "Staff Welfare", 156.00, "Front Desk"),
    (5, "Light bulbs x6 reception", "Maintenance", 42.00, "Ops Team"),
    (6, "Printer ink + paper", "Admin", 89.90, "Front Desk"),
    (8, "Grab delivery — urgent shampoo", "Transport", 18.00, "Groomer"),
    (9, "Welcome-kit ribbons & tags", "Marketing", 65.40, "Front Desk"),
]
for off, desc, cat, amt, by in PC:
    d = DAYS[off]
    db.add(M.PettyCashEntry(date=d, description=desc, category=cat, amount_out=amt,
                            month=mstr(d), recorded_by=by))

# ═══ 4. Sales — every day (recorded by whoever is on shift) ═══
BOARD_DESC = ["Premium room", "Royal suite", "Premium room x2", "Long-stay premium"]
sales_total = 0
for i, d in enumerate(DAYS):
    # boarding grows through the soft launch
    n_board = random.randint(1, 2) + (1 if i > 4 else 0)
    for _ in range(n_board):
        amt = random.choice([88, 88, 176, 228, 264, 440])
        db.add(M.SalesEntry(date=d, stream="Boarding", description=random.choice(BOARD_DESC),
                            amount=amt, method=random.choice(["Card", "TNG", "Bank Transfer"]),
                            month=mstr(d), recorded_by="Front Desk"))
        sales_total += amt
    for _ in range(random.randint(1, 3)):
        amt = random.choice([80, 120, 150, 150, 180, 250])
        db.add(M.SalesEntry(date=d, stream="Grooming", description="Full groom",
                            amount=amt, method=random.choice(["Cash", "Card", "TNG"]),
                            month=mstr(d), recorded_by="Groomer"))
        sales_total += amt
    if i in (3, 7):
        db.add(M.SalesEntry(date=d, stream="Retail", description="Cat accessories + treats",
                            amount=random.choice([65, 120, 240]), method="Cash",
                            month=mstr(d), recorded_by="Front Desk"))
    if i == 6:
        db.add(M.SalesEntry(date=d, stream="Membership", description="Founding member x2 (annual)",
                            amount=2400, method="Bank Transfer", month=mstr(d), recorded_by="Front Desk"))
    if i == 8:
        db.add(M.SalesEntry(date=d, stream="Cat Sales", description="Ragdoll kitten — deposit",
                            amount=2500, method="Bank Transfer", month=mstr(d), recorded_by="Front Desk"))

# ═══ 5. Confirmed payroll run for Jun 2026 ═══
run = M.PayrollRun(month="Jun 2026", run_date=date(2026, 6, 28), status="Confirmed")
db.add(run)
db.flush()
for s in db.query(M.Staff).filter(M.Staff.active == True).all():  # noqa: E712
    db.add(M.PayrollItem(run_id=run.id, staff_name=s.name, position=s.position,
                         base=s.base_salary, allowance=s.allowance,
                         epf_er=s.epf_employer, epf_ee=s.epf_employee,
                         socso_er=s.socso_employer, socso_ee=s.socso_employee,
                         eis_er=s.eis_employer, eis_ee=s.eis_employee))
db.flush()
run.total_net = sum(i.net for i in run.items)
run.total_cost = sum(i.employer_cost for i in run.items)

ver = db.get(M.Setting, "SAMPLE_DATA_VERSION")
if not ver:
    ver = M.Setting(key="SAMPLE_DATA_VERSION")
    db.add(ver)
ver.value = SAMPLE_VERSION

db.commit()
print(f"Sample data loaded: {len(PURCHASES)} verified docs+payments, 2 pending docs,")
print(f"3 vouchers (Paid/Approved/Draft), 1 listing, {len(PC)+1} petty cash entries,")
print(f"~RM {sales_total:,.0f} boarding+grooming sales over 10 days, Jun payroll confirmed.")
db.close()
