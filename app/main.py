import os
from datetime import date, datetime

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Depends, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import Base, engine, get_db, run_migrations
from . import models as M
from .auth import hash_password, verify_password, current_user
from . import telegram_bot, pdfgen, claude_ai
from .statutory import calc_statutory

Base.metadata.create_all(engine)
run_migrations()

app = FastAPI(title="CATDAY System")
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "catday-dev-secret"))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.filters["rm"] = lambda v: f"{(v or 0):,.2f}"
templates.env.filters["abs"] = lambda v: abs(v or 0)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "catdayhook")
BASE_URL = os.environ.get("BASE_URL", "https://catday-system.onrender.com").rstrip("/")
# URL-safe token derived from the secret — base64 secrets contain +/= which
# break URL path segments, so the webhook path uses this hex digest instead.
import hashlib as _hashlib
WEBHOOK_TOKEN = _hashlib.sha256(WEBHOOK_SECRET.encode()).hexdigest()[:40]

# Grouped navigation: (group label, [(key, url, icon, label, roles), ...])
NAV_GROUPS = [
    ("", [
        ("dashboard", "/", "home", "Dashboard", ("admin", "manager", "staff")),
    ]),
    ("Payables 应付", [
        ("documents", "/documents", "inbox", "Verification", ("admin", "manager")),
        ("payments", "/payments", "card", "Payments", ("admin", "manager")),
        ("suppliers", "/suppliers", "landmark", "Suppliers", ("admin", "manager")),
        ("vouchers", "/vouchers", "receipt", "Vouchers", ("admin", "manager")),
        ("listings", "/listings", "list", "Listings", ("admin", "manager")),
        ("pettycash", "/pettycash", "coins", "Petty Cash", ("admin", "manager", "staff")),
    ]),
    ("Income 收入", [
        ("sales", "/sales", "cart", "Sales", ("admin", "manager", "staff")),
        ("boarding", "/boarding", "cat", "Boarding", ("admin", "manager", "staff")),
    ]),
    ("People 人事", [
        ("payroll", "/payroll", "banknote", "Payroll", ("admin",)),
    ]),
    ("Reports 报告", [
        ("gl", "/reports/gl", "list", "General Ledger", ("admin", "manager")),
        ("pnl", "/pnl", "chart", "P&L", ("admin",)),
        ("apaging", "/reports/ap-aging", "list", "AP Aging", ("admin", "manager")),
        ("statutory", "/reports/statutory", "landmark", "Statutory", ("admin",)),
        ("tax", "/reports/tax", "receipt", "SST / Tax", ("admin", "manager")),
        ("reconciliation", "/reconciliation", "banknote", "Bank Reconciliation", ("admin", "manager")),
        ("einvoice", "/reports/einvoice-readiness", "receipt", "e-Invoice Readiness", ("admin",)),
    ]),
    ("Setup", [
        ("settings", "/settings", "settings", "Settings", ("admin",)),
    ]),
]
# Flat lookup for role checks
NAV = [item for _, items in NAV_GROUPS for item in items]


def render(request: Request, db: Session, template: str, page: str, **ctx):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    allowed = next((roles for key, _, _, _, roles in NAV if key == page), ())
    if user.role not in allowed:
        return RedirectResponse("/", status_code=302)
    nav_groups = []
    for glabel, items in NAV_GROUPS:
        visible = [(key, url, icon, label) for key, url, icon, label, roles in items
                   if user.role in roles]
        if visible:
            nav_groups.append((glabel, visible))
    pending_docs = db.query(M.Document).filter(M.Document.status == "Pending").count() \
        if user.role in ("admin", "manager") else 0
    return templates.TemplateResponse(request, template,
        {"user": user, "nav_groups": nav_groups, "page": page, "M": M, "today": date.today(),
         "pending_docs": pending_docs, **ctx})


def month_str(d: date | None = None) -> str:
    return f"{d or date.today():%b %Y}"


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date() if s else date.today()


def tax_of(tax_type: str, amount: float) -> float:
    """SST amount contained within a gross amount (tax-inclusive)."""
    rate = M.TAX_TYPES.get(tax_type, 0.0)
    if not rate:
        return 0.0
    return round(amount - amount / (1 + rate), 2)


def find_supplier(db: Session, name: str) -> M.Supplier | None:
    """Case-insensitive match of a voucher payee / payment supplier to the directory."""
    if not name:
        return None
    return db.query(M.Supplier).filter(func.lower(M.Supplier.name) == name.strip().lower(),
                                       M.Supplier.active == True).first()  # noqa: E712


def supplier_map(db: Session, names) -> dict:
    """{lowercased name: Supplier} for a set of payee names."""
    wanted = {str(n).strip().lower() for n in names if n}
    if not wanted:
        return {}
    out = {}
    for s in db.query(M.Supplier).filter(M.Supplier.active == True).all():  # noqa: E712
        if s.name.strip().lower() in wanted:
            out[s.name.strip().lower()] = s
    return out


# ─────────────────────────── AUTH (passcode) ───────────────────────────
def _login_ctx(db: Session, error: str = ""):
    profiles = db.query(M.User).filter(M.User.active == True).order_by(M.User.id).all()  # noqa: E712
    return {"profiles": profiles, "error": error}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "login.html", _login_ctx(db))


@app.post("/login")
def login(request: Request, passcode: str = Form(...), user_id: int = Form(...),
          db: Session = Depends(get_db)):
    setting = db.get(M.Setting, "PASSCODE")
    expected = (setting.value if setting else "") or os.environ.get("PASSCODE", "125180")
    user = db.get(M.User, user_id)
    if passcode.strip() != expected or not user or not user.active:
        return templates.TemplateResponse(request, "login.html",
                                          _login_ctx(db, "Wrong passcode  密码错误"))
    request.session["uid"] = user.id
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ─────────────────────────── DASHBOARD ───────────────────────────
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    mo = month_str()
    petty_bal = (db.query(func.coalesce(func.sum(M.PettyCashEntry.amount_in), 0)).scalar()
                 - db.query(func.coalesce(func.sum(M.PettyCashEntry.amount_out), 0)).scalar())
    stats = {
        "docs_pending": db.query(M.Document).filter(M.Document.status == "Pending").count(),
        "pay_open": db.query(M.Payment).filter(M.Payment.status.in_(["Unsorted", "Categorized"])).count(),
        "pv_draft": db.query(M.Voucher).filter(M.Voucher.status == "Draft").count(),
        "sales_month": db.query(func.coalesce(func.sum(M.SalesEntry.amount), 0))
            .filter(M.SalesEntry.month == mo).scalar(),
        "expenses_month": db.query(func.coalesce(func.sum(M.Payment.amount), 0))
            .filter(M.Payment.month == mo).scalar(),
        "petty_balance": petty_bal,
    }
    recent_docs = db.query(M.Document).order_by(M.Document.id.desc()).limit(8).all()
    recent_sales = db.query(M.SalesEntry).order_by(M.SalesEntry.id.desc()).limit(8).all()
    return render(request, db, "dashboard.html", "dashboard",
                  stats=stats, recent_docs=recent_docs, recent_sales=recent_sales, month=mo)


# ─────────────────────────── DOCUMENTS (VERIFICATION) ───────────────────────────
@app.get("/documents", response_class=HTMLResponse)
def documents(request: Request, view: str = "pending", db: Session = Depends(get_db)):
    import json as _json
    pending = db.query(M.Document).filter(M.Document.status == "Pending") \
        .order_by(M.Document.id).all()
    q = db.query(M.Document).filter(M.Document.status != "Pending") \
        .order_by(M.Document.id.desc())
    processed = q.limit(200).all()
    # Decode report payloads for the template
    payloads = {}
    for d in pending:
        if d.payload_json:
            try:
                payloads[d.id] = _json.loads(d.payload_json)
            except Exception:
                payloads[d.id] = {}
    return render(request, db, "documents.html", "documents",
                  pending=pending, processed=processed, view=view, payloads=payloads)


@app.post("/documents/upload")
async def upload_document(request: Request, file: UploadFile = File(...),
                          description: str = Form(""), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    data = await file.read()
    mime = file.content_type or "application/octet-stream"
    cls = claude_ai.classify(data, mime, description, file.filename or "upload")
    doc_no = telegram_bot.next_counter(db, "DOC", "DOC-")
    subdir = f"{date.today():%Y-%m}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel = f"{subdir}/{doc_no}_{pdfgen.safe_name(file.filename or 'upload', 60)}"
    ext = os.path.splitext(file.filename or "")[1]
    if ext and not rel.endswith(ext):
        rel += ext
    with open(os.path.join(UPLOAD_DIR, rel), "wb") as f:
        f.write(data)
    db.add(M.Document(
        doc_no=doc_no, sender=user.display_name, section=cls.get("section", "Expense"),
        doc_type=cls.get("doc_type", "Other"), supplier=cls.get("supplier", ""),
        amount=cls.get("amount", 0), month=cls.get("month") or month_str(),
        description=cls.get("description") or description, category=cls.get("category", ""),
        file_path=rel, mime=mime, ai_classified=cls.get("ai", False), status="Pending"))
    db.commit()
    return RedirectResponse("/documents", status_code=302)


@app.post("/documents/{doc_id}/verify")
async def verify_document(doc_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    doc = db.get(M.Document, doc_id)
    if not doc or doc.status != "Pending":
        return RedirectResponse("/documents", status_code=302)

    f = await request.form()
    section = str(f.get("section", doc.section))
    supplier = str(f.get("supplier", "")).strip()
    amount = float(f.get("amount") or 0)
    month = str(f.get("month", "")).strip()
    description = str(f.get("description", "")).strip()
    category = str(f.get("category", "")).strip()
    invoice_no = str(f.get("invoice_no", "")).strip()

    doc.section = section
    doc.doc_type = str(f.get("doc_type", doc.doc_type))
    doc.supplier, doc.amount, doc.month = supplier, amount, month or month_str()
    doc.description, doc.category, doc.invoice_no = description, category, invoice_no
    doc.status, doc.verified_by, doc.verified_at = "Verified", user.display_name, datetime.utcnow()

    # Route to the right module
    if section in ("Purchase", "Expense", "Staff Claim"):
        pay_no = telegram_bot.next_counter(db, "PAY", "PAY-")
        if section == "Staff Claim":
            grp, category = "OPEX", "Staff Claim"
            supplier = supplier or doc.sender   # claimant is reimbursed
        else:
            grp = M.group_for(category, section)   # cat-hotel category → P&L group
        p = M.Payment(pay_no=pay_no, supplier=supplier, description=description,
                      category=category, grp=grp, amount=amount, month=doc.month,
                      invoice_no=invoice_no,
                      status="Categorized" if category else "Unsorted",
                      notes=f"from {doc.doc_no} ({doc.sender})")
        db.add(p)
        db.flush()
        doc.payment_id = p.id
    elif section == "Petty Cash":
        db.add(M.PettyCashEntry(date=date.today(), description=description or doc.doc_no,
                                category=category, amount_out=amount, month=doc.month,
                                recorded_by=user.display_name, document_id=doc.id))
    elif section == "Sales Report":
        rdate = parse_date(str(f.get("rdate", "")))
        total = 0.0
        for stream in M.STREAMS:
            val = float(f.get(f"sales_{stream}") or 0)
            if val:
                db.add(M.SalesEntry(date=rdate, stream=stream,
                                    description=f"Daily report ({doc.doc_no})",
                                    amount=val, method="Mixed", month=month_str(rdate),
                                    recorded_by=doc.sender))
                total += val
        doc.amount = total
    elif section == "Boarding Log":
        db.add(M.BoardingLog(
            date=parse_date(str(f.get("rdate", ""))),
            checked_in=int(float(f.get("checked_in") or 0)),
            checked_out=int(float(f.get("checked_out") or 0)),
            occupancy=int(float(f.get("occupancy") or 0)),
            notes=description, recorded_by=doc.sender))
    # Bank-in Slip / Payroll / Filing Only → filed, no transaction record
    db.commit()
    return RedirectResponse("/documents", status_code=302)


@app.post("/documents/{doc_id}/reject")
def reject_document(doc_id: int, request: Request, reason: str = Form(""),
                    db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    doc = db.get(M.Document, doc_id)
    if doc and doc.status == "Pending":
        doc.status, doc.reject_reason = "Rejected", reason
        doc.verified_by, doc.verified_at = user.display_name, datetime.utcnow()
        db.commit()
    return RedirectResponse("/documents", status_code=302)


@app.get("/files/{path:path}")
def serve_file(path: str, request: Request, db: Session = Depends(get_db)):
    if not current_user(request, db):
        return RedirectResponse("/login", status_code=302)
    full = os.path.join(UPLOAD_DIR, path)
    if not os.path.isfile(full):
        raise HTTPException(404)
    return FileResponse(full, filename=os.path.basename(full),
                        content_disposition_type="inline")


# ─────────────────────────── PAYMENTS ───────────────────────────
ERRORS = {
    "mixed": "One voucher pays ONE company only — the payments you ticked belong to different suppliers. Create a separate voucher per supplier. 一张凭单只能支付一家公司。",
    "payee_mismatch": "The payee name doesn't match the supplier of the selected payments. Leave payee blank to use the supplier automatically.",
}


@app.get("/payments", response_class=HTMLResponse)
def payments(request: Request, status: str = "", error: str = "", db: Session = Depends(get_db)):
    q = db.query(M.Payment).order_by(M.Payment.id.desc())
    if status:
        q = q.filter(M.Payment.status == status)
    open_total = db.query(func.coalesce(func.sum(M.Payment.amount), 0)) \
        .filter(M.Payment.status.in_(["Unsorted", "Categorized"])).scalar()
    supplier_names = [s.name for s in db.query(M.Supplier)
                      .filter(M.Supplier.active == True).order_by(M.Supplier.name).all()]  # noqa: E712
    return render(request, db, "payments.html", "payments",
                  payments=q.limit(300).all(), flt=status, open_total=open_total,
                  supplier_names=supplier_names, error=ERRORS.get(error, ""))


@app.post("/payments/new")
def new_payment(request: Request, supplier: str = Form(""), description: str = Form(...),
                category: str = Form(""), grp: str = Form(""), amount: float = Form(...),
                invoice_no: str = Form(""), tax_type: str = Form("None"),
                pdate: str = Form(""), db: Session = Depends(get_db)):
    d = parse_date(pdate)
    pay_no = telegram_bot.next_counter(db, "PAY", "PAY-")
    db.add(M.Payment(pay_no=pay_no, date=d, supplier=supplier, description=description,
                     category=category, grp=grp, amount=amount, month=month_str(d),
                     invoice_no=invoice_no.strip(), tax_type=tax_type,
                     tax_amount=tax_of(tax_type, amount),
                     status="Categorized" if category else "Unsorted", notes="manual entry"))
    db.commit()
    return RedirectResponse("/payments", status_code=302)


@app.post("/payments/{pid}/update")
def update_payment(pid: int, request: Request, supplier: str = Form(""),
                   category: str = Form(""), grp: str = Form(""),
                   amount: float = Form(0), db: Session = Depends(get_db)):
    p = db.get(M.Payment, pid)
    if p and p.status in ("Unsorted", "Categorized"):
        p.supplier, p.category, p.grp, p.amount = supplier, category, grp, amount
        p.status = "Categorized" if category else "Unsorted"
        db.commit()
    return RedirectResponse("/payments", status_code=302)


# ─────────────────────────── SUPPLIERS ───────────────────────────
@app.get("/suppliers", response_class=HTMLResponse)
def suppliers(request: Request, db: Session = Depends(get_db)):
    sups = db.query(M.Supplier).order_by(M.Supplier.name).all()
    return render(request, db, "suppliers.html", "suppliers", suppliers=sups)


@app.get("/suppliers/{sid}", response_class=HTMLResponse)
def supplier_detail(sid: int, request: Request, db: Session = Depends(get_db)):
    s = db.get(M.Supplier, sid)
    if not s:
        return RedirectResponse("/suppliers", status_code=302)
    pays = db.query(M.Payment).filter(func.lower(M.Payment.supplier) == s.name.lower()) \
        .order_by(M.Payment.date.desc()).all()
    total_all = sum(p.amount for p in pays)
    paid = sum(p.amount for p in pays if p.status == "Paid")
    outstanding = sum(p.amount for p in pays if p.status in ("Unsorted", "Categorized", "On Voucher"))
    docs = db.query(M.Document).filter(func.lower(M.Document.supplier) == s.name.lower(),
                                       M.Document.file_path != "") \
        .order_by(M.Document.id.desc()).limit(50).all()
    return render(request, db, "supplier_detail.html", "suppliers", s=s, pays=pays,
                  total_all=total_all, paid=paid, outstanding=outstanding, docs=docs)


@app.post("/suppliers/new")
def supplier_new(request: Request, name: str = Form(...), sup_type: str = Form("Supplier"),
                 bank_name: str = Form(""), account_no: str = Form(""),
                 account_holder: str = Form(""), contact_person: str = Form(""),
                 phone: str = Form(""), email: str = Form(""), notes: str = Form(""),
                 tin: str = Form(""), brn: str = Form(""),
                 db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    if not db.query(M.Supplier).filter(func.lower(M.Supplier.name) == name.strip().lower()).first():
        db.add(M.Supplier(name=name.strip(), sup_type=sup_type, bank_name=bank_name.strip(),
                          account_no=account_no.strip(), account_holder=account_holder.strip(),
                          contact_person=contact_person, phone=phone, email=email, notes=notes,
                          tin=tin.strip(), brn=brn.strip()))
        db.commit()
    return RedirectResponse("/suppliers", status_code=302)


@app.post("/suppliers/{sid}/update")
def supplier_update(sid: int, request: Request, name: str = Form(...), sup_type: str = Form("Supplier"),
                    bank_name: str = Form(""), account_no: str = Form(""),
                    account_holder: str = Form(""), contact_person: str = Form(""),
                    phone: str = Form(""), email: str = Form(""), notes: str = Form(""),
                    tin: str = Form(""), brn: str = Form(""),
                    active: str = Form(""), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    s = db.get(M.Supplier, sid)
    if s:
        s.name, s.sup_type = name.strip(), sup_type
        s.bank_name, s.account_no, s.account_holder = bank_name.strip(), account_no.strip(), account_holder.strip()
        s.contact_person, s.phone, s.email, s.notes = contact_person, phone, email, notes
        s.tin, s.brn = tin.strip(), brn.strip()
        s.active = active == "on"
        db.commit()
    return RedirectResponse("/suppliers", status_code=302)


# ─────────────────────────── VOUCHERS ───────────────────────────
@app.get("/vouchers", response_class=HTMLResponse)
def vouchers(request: Request, q: str = "", status: str = "", db: Session = Depends(get_db)):
    query = db.query(M.Voucher).order_by(M.Voucher.id.desc())
    if status:
        query = query.filter(M.Voucher.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((M.Voucher.pv_no.ilike(like)) | (M.Voucher.payee.ilike(like)))
    pvs = query.limit(300).all()
    banks = supplier_map(db, [v.payee for v in pvs])
    return render(request, db, "vouchers.html", "vouchers", vouchers=pvs, banks=banks,
                  q=q, flt=status, pv_status=M.PV_STATUS)


@app.post("/vouchers/create")
async def create_voucher(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    form = await request.form()
    ids = [int(x) for v in form.getlist("pay_ids") for x in str(v).split(",") if x.strip()]
    payee = str(form.get("payee", "")).strip()
    pays = db.query(M.Payment).filter(M.Payment.id.in_(ids),
                                      M.Payment.status.in_(["Unsorted", "Categorized"])).all()
    if not pays:
        return RedirectResponse("/payments", status_code=302)

    # Accounting rule: one voucher pays exactly one company/person.
    distinct = {(p.supplier or "").strip().lower() for p in pays}
    if len(distinct) > 1:
        return RedirectResponse("/payments?error=mixed", status_code=302)
    supplier_name = pays[0].supplier or ""
    if payee and supplier_name and payee.strip().lower() != supplier_name.strip().lower():
        return RedirectResponse("/payments?error=payee_mismatch", status_code=302)
    payee = payee or supplier_name or "Payee"

    pv_no = telegram_bot.next_counter(db, "PV", "PV-")
    total = sum(p.amount for p in pays)
    items = [{"date": f"{p.date:%d/%m/%y}", "description": p.description, "amount": p.amount,
              "invoice_no": p.invoice_no,
              "doc_url": f"{BASE_URL}/files/{p.documents[0].file_path}" if p.documents else ""}
             for p in pays]
    settings = {s.key: s.value for s in db.query(M.Setting).all()}
    sup = find_supplier(db, payee)
    bank = ({"bank_name": sup.bank_name, "account_no": sup.account_no,
             "account_holder": sup.account_holder} if sup else None)
    rel = pdfgen.voucher_pdf(pv_no, payee, items, total,
                             company=settings.get("COMPANY_NAME", "CATDAY SDN BHD"),
                             address=settings.get("COMPANY_ADDRESS", "Uptown PJ"),
                             bank=bank)
    pv = M.Voucher(pv_no=pv_no, payee=payee, total=total, pdf_path=rel,
                   created_by=user.display_name if user else "")
    db.add(pv)
    db.flush()
    for p in pays:
        p.voucher_id = pv.id
        p.status = "On Voucher"
    db.commit()
    return RedirectResponse("/vouchers", status_code=302)


@app.post("/vouchers/{vid}/action")
def voucher_action(vid: int, request: Request, action: str = Form(...),
                   db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    v = db.get(M.Voucher, vid)
    if v:
        if action == "approve" and v.status == "Draft":
            v.status, v.approved_by = "Approved", user.display_name
        elif action == "paid" and v.status in ("Draft", "Approved"):
            v.status = "Paid"
            for p in v.payments:
                p.status = "Paid"
        elif action == "void" and v.status != "Paid":
            v.status = "Void"
            for p in v.payments:
                p.status, p.voucher_id = "Categorized" if p.category else "Unsorted", None
        db.commit()
    return RedirectResponse("/vouchers", status_code=302)


# ─────────────────────────── LISTINGS ───────────────────────────
@app.get("/listings", response_class=HTMLResponse)
def listings(request: Request, q: str = "", status: str = "", db: Session = Depends(get_db)):
    query = db.query(M.Listing).order_by(M.Listing.id.desc())
    if status:
        query = query.filter(M.Listing.status == status)
    if q:
        query = query.filter(M.Listing.pl_no.ilike(f"%{q.strip()}%"))
    pls = query.limit(300).all()
    names = [v.payee for pl in pls for v in pl.vouchers]
    banks = supplier_map(db, names)
    return render(request, db, "listings.html", "listings", listings=pls, banks=banks,
                  q=q, flt=status, pl_status=M.PL_STATUS)


@app.post("/listings/create")
async def create_listing(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    form = await request.form()
    ids = [int(x) for v in form.getlist("pv_ids") for x in str(v).split(",") if x.strip()]
    pvs = db.query(M.Voucher).filter(M.Voucher.id.in_(ids),
                                     M.Voucher.listing_id.is_(None),
                                     M.Voucher.status.in_(["Draft", "Approved"])).all()
    if not pvs:
        return RedirectResponse("/vouchers", status_code=302)
    pl_no = telegram_bot.next_counter(db, "PL", "PL-")
    total = sum(v.total for v in pvs)
    banks = supplier_map(db, [v.payee for v in pvs])
    def bank_line(payee):
        s = banks.get(payee.strip().lower())
        return f"{s.bank_name} {s.account_no}" if s and (s.bank_name or s.account_no) else ""
    vdata = [{"pv_no": v.pv_no, "date": f"{v.date:%d/%m/%y}", "payee": v.payee,
              "total": v.total, "bank": bank_line(v.payee)}
             for v in pvs]
    settings = {s.key: s.value for s in db.query(M.Setting).all()}
    rel = pdfgen.listing_pdf(pl_no, vdata, total,
                             company=settings.get("COMPANY_NAME", "CATDAY SDN BHD"),
                             address=settings.get("COMPANY_ADDRESS", "Uptown PJ"))
    pl = M.Listing(pl_no=pl_no, total=total, pdf_path=rel,
                   prepared_by=user.display_name if user else "")
    db.add(pl)
    db.flush()
    for v in pvs:
        v.listing_id = pl.id
    db.commit()
    return RedirectResponse("/listings", status_code=302)


@app.get("/listings/{lid}/bank-file")
def listing_bank_file(lid: int, bank: str, request: Request, db: Session = Depends(get_db)):
    """Generate a bulk-transfer file for the chosen Malaysian bank from a listing.
    One row per voucher (one payee = one payment), using the supplier's bank details."""
    if not current_user(request, db):
        return RedirectResponse("/login", status_code=302)
    pl = db.get(M.Listing, lid)
    if not pl:
        raise HTTPException(404)
    fmt = M.MY_BANK_FORMATS.get(bank)
    if not fmt:
        raise HTTPException(400, "Unknown bank format")
    sup_by = supplier_map(db, [v.payee for v in pl.vouchers])

    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fmt["cols"])
    for v in pl.vouchers:
        s = sup_by.get(v.payee.strip().lower())
        acct = (s.account_no if s else "").replace(" ", "")
        holder = (s.account_holder if s and s.account_holder else v.payee)
        bcode = M.MY_BANK_CODES.get(s.bank_name, "") if s else ""
        bname = s.bank_name if s else ""
        ref = v.pv_no
        # Map our fields onto whatever columns this bank uses
        row = []
        for col in fmt["cols"]:
            cl = col.lower()
            if "type" in cl:
                row.append("IBG")
            elif "name" in cl or "holder" in cl:
                row.append(holder)
            elif "account" in cl or "account no" in cl or cl == "account number":
                row.append(acct)
            elif "bank code" in cl:
                row.append(bcode)
            elif cl == "bank" or "bank name" in cl:
                row.append(bname or bcode)
            elif "amount" in cl:
                row.append(f"{v.total:.2f}")
            elif "email" in cl:
                row.append(s.email if s and s.email else "")
            elif "description" in cl or "remark" in cl:
                row.append(f"Payment {v.pv_no}")
            elif "ref" in cl:
                row.append(ref)
            else:
                row.append("")
        w.writerow(row)

    from fastapi.responses import Response
    fname = f"{pl.pl_no}_{fmt['code']}_bulk.csv"
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/listings/{lid}/action")
def listing_action(lid: int, request: Request, action: str = Form(...),
                   db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    pl = db.get(M.Listing, lid)
    if pl:
        if action == "submit" and pl.status == "Draft":
            pl.status = "Submitted"
        elif action == "processed" and pl.status in ("Draft", "Submitted"):
            pl.status = "Processed"
        db.commit()
    return RedirectResponse("/listings", status_code=302)


# ─────────────────────────── PETTY CASH (multi-account) ───────────────────────────
def _ensure_default_pc_account(db: Session):
    if db.query(M.PettyCashAccount).count() == 0:
        settings = {s.key: s.value for s in db.query(M.Setting).all()}
        ft = float(settings.get("PETTY_CASH_FLOAT", "5000") or 5000)
        db.add(M.PettyCashAccount(name="Main Float", float_target=ft))
        db.commit()


@app.get("/pettycash", response_class=HTMLResponse)
def pettycash(request: Request, account: int = 0, month: str = "", db: Session = Depends(get_db)):
    _ensure_default_pc_account(db)
    accounts = db.query(M.PettyCashAccount).order_by(M.PettyCashAccount.id).all()
    acc = db.get(M.PettyCashAccount, account) if account else accounts[0]
    if not acc:
        acc = accounts[0]

    entries = db.query(M.PettyCashEntry).filter(
        (M.PettyCashEntry.account_id == acc.id) |
        ((M.PettyCashEntry.account_id.is_(None)) & (acc.id == accounts[0].id))  # legacy → first acct
    ).order_by(M.PettyCashEntry.date, M.PettyCashEntry.id).all()
    bal = 0.0
    rows = []
    for e in entries:
        bal += e.amount_in - e.amount_out
        rows.append((e, bal))
    mo = month or month_str()
    month_rows = [(e, b) for e, b in rows if e.month == mo] if month else rows
    months = sorted({e.month for e in entries if e.month} | {month_str()})
    float_target = acc.float_target
    mo_out = sum(e.amount_out for e, _ in month_rows)
    mo_in = sum(e.amount_in for e, _ in month_rows)
    by_cat = {}
    for e, _ in month_rows:
        if e.amount_out:
            by_cat[e.category or "Uncategorized"] = by_cat.get(e.category or "Uncategorized", 0) + e.amount_out
    display = list(reversed(month_rows))
    return render(request, db, "pettycash.html", "pettycash",
                  rows=display, balance=bal, float_target=float_target,
                  months=months, month=mo, month_filtered=bool(month),
                  mo_out=mo_out, mo_in=mo_in, by_cat=by_cat,
                  accounts=accounts, acc=acc)


@app.post("/pettycash/account/new")
def pettycash_account_new(request: Request, name: str = Form(...),
                          float_target: float = Form(5000), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    if name.strip() and not db.query(M.PettyCashAccount).filter(
            func.lower(M.PettyCashAccount.name) == name.strip().lower()).first():
        db.add(M.PettyCashAccount(name=name.strip(), float_target=float_target or 0))
        db.commit()
    return RedirectResponse("/pettycash", status_code=302)


@app.post("/pettycash/new")
def pettycash_new(request: Request, description: str = Form(...), category: str = Form(""),
                  amount_out: float = Form(0), amount_in: float = Form(0),
                  account_id: int = Form(0), pdate: str = Form(""), db: Session = Depends(get_db)):
    user = current_user(request, db)
    d = parse_date(pdate)
    db.add(M.PettyCashEntry(date=d, description=description, category=category,
                            amount_out=amount_out or 0, amount_in=amount_in or 0,
                            month=month_str(d), account_id=account_id or None,
                            recorded_by=user.display_name if user else ""))
    db.commit()
    return RedirectResponse(f"/pettycash?account={account_id}" if account_id else "/pettycash",
                            status_code=302)


# ─────────────────────────── SALES ───────────────────────────
@app.get("/sales", response_class=HTMLResponse)
def sales(request: Request, db: Session = Depends(get_db)):
    entries = db.query(M.SalesEntry).order_by(M.SalesEntry.id.desc()).limit(300).all()
    mo = month_str()
    by_stream = dict(db.query(M.SalesEntry.stream, func.sum(M.SalesEntry.amount))
                     .filter(M.SalesEntry.month == mo).group_by(M.SalesEntry.stream).all())
    return render(request, db, "sales.html", "sales", entries=entries,
                  by_stream=by_stream, month=mo)


@app.post("/sales/new")
def sales_new(request: Request, stream: str = Form(...), description: str = Form(""),
              amount: float = Form(...), method: str = Form("Cash"),
              tax_type: str = Form("None"), pdate: str = Form(""),
              db: Session = Depends(get_db)):
    user = current_user(request, db)
    d = parse_date(pdate)
    db.add(M.SalesEntry(date=d, stream=stream, description=description, amount=amount,
                        method=method, month=month_str(d), tax_type=tax_type,
                        tax_amount=tax_of(tax_type, amount),
                        recorded_by=user.display_name if user else ""))
    db.commit()
    return RedirectResponse("/sales", status_code=302)


# ─────────────────────────── BOARDING ───────────────────────────
@app.get("/boarding", response_class=HTMLResponse)
def boarding(request: Request, db: Session = Depends(get_db)):
    logs = db.query(M.BoardingLog).order_by(M.BoardingLog.date.desc(), M.BoardingLog.id.desc()).limit(120).all()
    latest = logs[0] if logs else None
    mo = month_str()
    mo_in = sum(l.checked_in for l in logs if month_str(l.date) == mo)
    mo_out = sum(l.checked_out for l in logs if month_str(l.date) == mo)
    return render(request, db, "boarding.html", "boarding",
                  logs=logs, latest=latest, mo_in=mo_in, mo_out=mo_out, month=mo)


@app.post("/boarding/new")
def boarding_new(request: Request, bdate: str = Form(""), checked_in: int = Form(0),
                 checked_out: int = Form(0), occupancy: int = Form(0),
                 notes: str = Form(""), db: Session = Depends(get_db)):
    user = current_user(request, db)
    db.add(M.BoardingLog(date=parse_date(bdate), checked_in=checked_in or 0,
                         checked_out=checked_out or 0, occupancy=occupancy or 0,
                         notes=notes, recorded_by=user.display_name if user else ""))
    db.commit()
    return RedirectResponse("/boarding", status_code=302)


# ─────────────────────────── PAYROLL ───────────────────────────
@app.get("/payroll", response_class=HTMLResponse)
def payroll(request: Request, db: Session = Depends(get_db)):
    staff = db.query(M.Staff).order_by(M.Staff.id).all()
    runs = db.query(M.PayrollRun).order_by(M.PayrollRun.id.desc()).all()
    active = [s for s in staff if s.active]
    totals = {
        "gross": sum(s.gross for s in active),
        "net": sum(s.net_pay for s in active),
        "cost": sum(s.employer_cost for s in active),
    }
    return render(request, db, "payroll.html", "payroll",
                  staff=staff, runs=runs, totals=totals)


def _apply_statutory(s: M.Staff):
    st = calc_statutory(s.base_salary + s.allowance)
    s.epf_employer, s.epf_employee = st["epf_er"], st["epf_ee"]
    s.socso_employer, s.socso_employee = st["socso_er"], st["socso_ee"]
    s.eis_employer, s.eis_employee = st["eis_er"], st["eis_ee"]


@app.post("/payroll/staff/new")
def staff_new(request: Request, name: str = Form(...), position: str = Form(""),
              base_salary: float = Form(0), allowance: float = Form(0),
              db: Session = Depends(get_db)):
    s = M.Staff(name=name, position=position, base_salary=base_salary, allowance=allowance)
    _apply_statutory(s)
    db.add(s)
    db.commit()
    return RedirectResponse("/payroll", status_code=302)


@app.post("/payroll/staff/{sid}/update")
def staff_update(sid: int, request: Request, name: str = Form(...), position: str = Form(""),
                 base_salary: float = Form(0), allowance: float = Form(0),
                 active: str = Form(""), db: Session = Depends(get_db)):
    s = db.get(M.Staff, sid)
    if s:
        s.name, s.position = name, position
        s.base_salary, s.allowance = base_salary, allowance
        _apply_statutory(s)   # EPF/SOCSO/EIS always follow the latest salary
        s.active = active == "on"
        db.commit()
    return RedirectResponse("/payroll", status_code=302)


@app.post("/payroll/run")
def payroll_run(month: str = Form(...), db: Session = Depends(get_db)):
    existing = db.query(M.PayrollRun).filter(M.PayrollRun.month == month,
                                             M.PayrollRun.status == "Draft").first()
    if existing:
        return RedirectResponse(f"/payroll/run/{existing.id}", status_code=302)
    run = M.PayrollRun(month=month)
    db.add(run)
    db.flush()
    for s in db.query(M.Staff).filter(M.Staff.active == True).all():  # noqa: E712
        st = calc_statutory(s.base_salary + s.allowance)
        db.add(M.PayrollItem(run_id=run.id, staff_name=s.name, position=s.position,
                             base=s.base_salary, allowance=s.allowance,
                             epf_er=st["epf_er"], epf_ee=st["epf_ee"],
                             socso_er=st["socso_er"], socso_ee=st["socso_ee"],
                             eis_er=st["eis_er"], eis_ee=st["eis_ee"]))
    db.flush()
    run.total_net = sum(i.net for i in run.items)
    run.total_cost = sum(i.employer_cost for i in run.items)
    db.commit()
    return RedirectResponse(f"/payroll/run/{run.id}", status_code=302)


@app.get("/payroll/run/{rid}", response_class=HTMLResponse)
def payroll_run_view(rid: int, request: Request, db: Session = Depends(get_db)):
    run = db.get(M.PayrollRun, rid)
    if not run:
        return RedirectResponse("/payroll", status_code=302)
    return render(request, db, "payroll_run.html", "payroll", run=run)


@app.post("/payroll/run/{rid}/item/{iid}/update")
def payroll_item_update(rid: int, iid: int, request: Request,
                        base: float = Form(0), allowance: float = Form(0),
                        overtime: float = Form(0), commission: float = Form(0),
                        bonus: float = Form(0), unpaid_leave_days: float = Form(0),
                        pcb: float = Form(0), deductions: float = Form(0),
                        remarks: str = Form(""), db: Session = Depends(get_db)):
    run = db.get(M.PayrollRun, rid)
    item = db.get(M.PayrollItem, iid)
    if run and item and item.run_id == rid and run.status == "Draft":
        item.base, item.allowance, item.overtime, item.bonus = base, allowance, overtime, bonus
        item.commission = commission
        # Unpaid leave deducts a pro-rata day rate (base / 26 working days)
        item.unpaid_leave_days = unpaid_leave_days
        item.leave_deduction = round((base / 26.0) * unpaid_leave_days, 2) if unpaid_leave_days else 0.0
        item.pcb, item.deductions, item.remarks = pcb, deductions, remarks
        # Statutory always recalculated from the latest gross
        st = calc_statutory(item.gross)
        item.epf_er, item.epf_ee = st["epf_er"], st["epf_ee"]
        item.socso_er, item.socso_ee = st["socso_er"], st["socso_ee"]
        item.eis_er, item.eis_ee = st["eis_er"], st["eis_ee"]
        run.total_net = sum(i.net for i in run.items)
        run.total_cost = sum(i.employer_cost for i in run.items)
        db.commit()
    return RedirectResponse(f"/payroll/run/{rid}", status_code=302)


@app.post("/payroll/run/{rid}/reopen")
def payroll_reopen(rid: int, request: Request, db: Session = Depends(get_db)):
    """Reopen a confirmed run for correction — payslips regenerate on next confirm."""
    user = current_user(request, db)
    run = db.get(M.PayrollRun, rid)
    if run and run.status == "Confirmed" and user and user.role == "admin":
        run.status = "Draft"
        db.commit()
    return RedirectResponse(f"/payroll/run/{rid}", status_code=302)


@app.post("/payroll/run/{rid}/confirm")
def payroll_confirm(rid: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    run = db.get(M.PayrollRun, rid)
    if run and run.status == "Draft" and user and user.role == "admin":
        run.status = "Confirmed"
        settings = {s.key: s.value for s in db.query(M.Setting).all()}
        for item in run.items:
            pdfgen.payslip_pdf(run.month, item,
                               company=settings.get("COMPANY_NAME", "CATDAY SDN BHD"),
                               address=settings.get("COMPANY_ADDRESS", "Uptown PJ"))
        db.commit()
    return RedirectResponse(f"/payroll/run/{rid}", status_code=302)


@app.post("/payroll/run/{rid}/delete")
def payroll_delete(rid: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    run = db.get(M.PayrollRun, rid)
    if run and run.status == "Draft" and user and user.role == "admin":
        db.delete(run)
        db.commit()
    return RedirectResponse("/payroll", status_code=302)


@app.get("/payroll/run/{rid}/payslip/{iid}")
def payslip_download(rid: int, iid: int, request: Request, db: Session = Depends(get_db)):
    if not current_user(request, db):
        return RedirectResponse("/login", status_code=302)
    run = db.get(M.PayrollRun, rid)
    item = db.get(M.PayrollItem, iid)
    if not run or not item or item.run_id != rid:
        raise HTTPException(404)
    settings = {s.key: s.value for s in db.query(M.Setting).all()}
    rel = pdfgen.payslip_pdf(run.month, item,
                             company=settings.get("COMPANY_NAME", "CATDAY SDN BHD"),
                             address=settings.get("COMPANY_ADDRESS", "Uptown PJ"))
    full = os.path.join(UPLOAD_DIR, rel)
    return FileResponse(full, filename=os.path.basename(full),
                        content_disposition_type="inline")


# ─────────────────────────── BANK RECONCILIATION ───────────────────────────
def _unmatched_system_txns(db: Session, bank_account_id: int):
    """Candidate book-side transactions not yet matched to any statement line."""
    matched = {(l.matched_type, l.matched_id) for l in
               db.query(M.BankStatementLine).filter(M.BankStatementLine.matched == True).all()}  # noqa: E712
    out = []
    for v in db.query(M.Voucher).filter(M.Voucher.status == "Paid").all():
        if ("Voucher", v.id) not in matched:
            out.append({"type": "Voucher", "id": v.id, "date": v.date, "party": v.payee,
                       "desc": f"{v.pv_no} · {v.payee}", "amount": -v.total})
    for s in db.query(M.SalesEntry).all():
        if ("Sale", s.id) not in matched:
            out.append({"type": "Sale", "id": s.id, "date": s.date, "party": s.stream,
                       "desc": f"{s.stream} · {s.description[:30]}", "amount": s.amount})
    for e in db.query(M.PettyCashEntry).filter(M.PettyCashEntry.amount_in > 0).all():
        if ("PettyCash", e.id) not in matched:
            out.append({"type": "PettyCash", "id": e.id, "date": e.date, "party": "Petty cash",
                       "desc": f"Top-up · {e.description[:30]}", "amount": -e.amount_in})
    for run in db.query(M.PayrollRun).filter(M.PayrollRun.status == "Confirmed").all():
        if ("Payroll", run.id) not in matched:
            out.append({"type": "Payroll", "id": run.id, "date": run.run_date,
                       "party": f"{len(run.items)} staff",
                       "desc": f"Payroll {run.month} · net pay to staff",
                       "amount": -run.total_net})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


@app.get("/reconciliation", response_class=HTMLResponse)
def reconciliation(request: Request, account: int = 0, db: Session = Depends(get_db)):
    accounts = db.query(M.BankAccount).filter(M.BankAccount.active == True).order_by(M.BankAccount.id).all()  # noqa: E712
    acc = db.get(M.BankAccount, account) if account else (accounts[0] if accounts else None)
    lines, candidates = [], []
    reconciled_total = unreconciled_total = 0.0
    if acc:
        all_lines = db.query(M.BankStatementLine).filter(
            M.BankStatementLine.bank_account_id == acc.id).order_by(
            M.BankStatementLine.date.desc(), M.BankStatementLine.id.desc()).all()
        lines = all_lines
        reconciled_total = sum(l.amount for l in all_lines if l.matched)
        unreconciled_total = sum(l.amount for l in all_lines if not l.matched)
        candidates = _unmatched_system_txns(db, acc.id)
    unmatched_count = sum(1 for l in lines if not l.matched)
    opening_balance = acc.opening_balance if acc else 0.0
    balance_per_bank = opening_balance + reconciled_total + unreconciled_total
    balance_per_books = opening_balance + reconciled_total
    return render(request, db, "reconciliation.html", "reconciliation",
                  accounts=accounts, acc=acc, lines=lines, candidates=candidates,
                  reconciled_total=reconciled_total, unreconciled_total=unreconciled_total,
                  unmatched_count=unmatched_count, opening_balance=opening_balance,
                  balance_per_bank=balance_per_bank, balance_per_books=balance_per_books)


@app.post("/reconciliation/account/new")
def reconciliation_account_new(request: Request, name: str = Form(...), bank_name: str = Form(""),
                               account_no: str = Form(""), opening_balance: float = Form(0),
                               db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    if name.strip() and not db.query(M.BankAccount).filter(
            func.lower(M.BankAccount.name) == name.strip().lower()).first():
        db.add(M.BankAccount(name=name.strip(), bank_name=bank_name, account_no=account_no,
                             opening_balance=opening_balance or 0))
        db.commit()
    return RedirectResponse("/reconciliation", status_code=302)


@app.post("/reconciliation/import")
async def reconciliation_import(request: Request, account_id: int = Form(...),
                                file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Import a bank statement CSV. Expected columns (case-insensitive, flexible order):
    Date, Description, Amount  — Amount: positive = money in, negative = money out.
    Also accepts separate Debit / Credit columns instead of a single Amount."""
    import csv, io, uuid
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    batch = uuid.uuid4().hex[:8]
    added = 0
    for row in reader:
        keys = {k.strip().lower(): k for k in row.keys() if k}
        def get(*names):
            for n in names:
                if n in keys and row[keys[n]].strip():
                    return row[keys[n]].strip()
            return ""
        d_raw = get("date")
        desc = get("description", "details", "particulars", "narrative")
        ref = get("reference", "ref", "cheque no")
        amt_raw = get("amount")
        debit = get("debit", "withdrawal")
        credit = get("credit", "deposit")
        try:
            d = parse_date(d_raw) if d_raw else date.today()
        except Exception:
            continue
        if amt_raw:
            try:
                amount = float(amt_raw.replace(",", ""))
            except ValueError:
                continue
        else:
            try:
                amount = (float(credit.replace(",", "")) if credit else 0.0) - \
                         (float(debit.replace(",", "")) if debit else 0.0)
            except ValueError:
                continue
        if amount == 0 and not desc:
            continue
        db.add(M.BankStatementLine(bank_account_id=account_id, date=d, description=desc,
                                   ref=ref, amount=amount, import_batch=batch))
        added += 1
    db.commit()
    return RedirectResponse(f"/reconciliation?account={account_id}", status_code=302)


@app.post("/reconciliation/match")
def reconciliation_match(request: Request, line_id: int = Form(...), txn_type: str = Form(...),
                         txn_id: int = Form(...), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    line = db.get(M.BankStatementLine, line_id)
    if line:
        line.matched, line.matched_type, line.matched_id = True, txn_type, txn_id
        db.commit()
    return RedirectResponse(f"/reconciliation?account={line.bank_account_id}" if line else "/reconciliation",
                            status_code=302)


@app.post("/reconciliation/unmatch/{line_id}")
def reconciliation_unmatch(line_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    line = db.get(M.BankStatementLine, line_id)
    if line:
        line.matched, line.matched_type, line.matched_id = False, "", None
        db.commit()
    return RedirectResponse(f"/reconciliation?account={line.bank_account_id}" if line else "/reconciliation",
                            status_code=302)


@app.post("/reconciliation/delete/{line_id}")
def reconciliation_delete_line(line_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    line = db.get(M.BankStatementLine, line_id)
    if line:
        acc_id = line.bank_account_id
        db.delete(line)
        db.commit()
        return RedirectResponse(f"/reconciliation?account={acc_id}", status_code=302)
    return RedirectResponse("/reconciliation", status_code=302)


# ─────────────────────────── e-INVOICE / MyInvois READINESS ───────────────────────────
@app.get("/reports/einvoice-readiness", response_class=HTMLResponse)
def einvoice_readiness(request: Request, db: Session = Depends(get_db)):
    settings = {s.key: s.value for s in db.query(M.Setting).all()}
    suppliers = db.query(M.Supplier).filter(M.Supplier.active == True).order_by(M.Supplier.name).all()  # noqa: E712
    with_tin = [s for s in suppliers if s.tin.strip()]
    without_tin = [s for s in suppliers if not s.tin.strip()]
    company_ready = bool(settings.get("COMPANY_TIN", "").strip()) and bool(settings.get("COMPANY_MSIC", "").strip())
    pct = round(len(with_tin) / len(suppliers) * 100) if suppliers else 0
    return render(request, db, "einvoice_readiness.html", "einvoice",
                  settings=settings, suppliers=suppliers, with_tin=with_tin,
                  without_tin=without_tin, company_ready=company_ready, pct=pct)


@app.post("/suppliers/{sid}/tin")
def supplier_update_tin(sid: int, request: Request, tin: str = Form(""), brn: str = Form(""),
                        db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    s = db.get(M.Supplier, sid)
    if s:
        s.tin, s.brn = tin.strip(), brn.strip()
        db.commit()
    return RedirectResponse("/reports/einvoice-readiness", status_code=302)


# ─────────────────────────── GENERAL LEDGER ───────────────────────────
@app.get("/reports/gl", response_class=HTMLResponse)
def general_ledger(request: Request, q: str = "", frm: str = "", to: str = "",
                   kind: str = "", db: Session = Depends(get_db)):
    """Unified searchable ledger of every money movement, by code/date/text/type."""
    ql = q.strip().lower()
    d_from = parse_date(frm) if frm else None
    d_to = parse_date(to) if to else None
    entries = []   # (date, code, type, party, description, money_in, money_out, link)

    def match(*fields):
        if not ql:
            return True
        return any(ql in str(f).lower() for f in fields)

    def in_range(dt):
        if d_from and dt < d_from:
            return False
        if d_to and dt > d_to:
            return False
        return True

    if kind in ("", "Payment"):
        for p in db.query(M.Payment).filter(M.Payment.status != "Void").all():
            if in_range(p.date) and match(p.pay_no, p.supplier, p.description, p.invoice_no, p.category):
                entries.append({"date": p.date, "code": p.pay_no, "type": "Payment",
                                "party": p.supplier, "desc": p.description,
                                "cin": 0, "cout": p.amount, "link": "/payments"})
    if kind in ("", "Sale"):
        for s in db.query(M.SalesEntry).all():
            if in_range(s.date) and match(s.stream, s.description, s.method):
                entries.append({"date": s.date, "code": s.stream, "type": "Sale",
                                "party": s.stream, "desc": s.description,
                                "cin": s.amount, "cout": 0, "link": "/sales",
                                "match_key": ("Sale", s.id)})
    if kind in ("", "Petty Cash"):
        for e in db.query(M.PettyCashEntry).all():
            if in_range(e.date) and match(e.description, e.category, e.recorded_by):
                entries.append({"date": e.date, "code": "PC", "type": "Petty Cash",
                                "party": e.recorded_by, "desc": e.description,
                                "cin": e.amount_in, "cout": e.amount_out, "link": "/pettycash",
                                "match_key": ("PettyCash", e.id)})
    if kind in ("", "Voucher"):
        for v in db.query(M.Voucher).all():
            if in_range(v.date) and match(v.pv_no, v.payee, v.status):
                entries.append({"date": v.date, "code": v.pv_no, "type": "Voucher",
                                "party": v.payee, "desc": f"Voucher · {v.status}",
                                "cin": 0, "cout": v.total, "link": "/vouchers",
                                "match_key": ("Voucher", v.id)})
    if kind in ("", "Listing"):
        for l in db.query(M.Listing).all():
            if in_range(l.date) and match(l.pl_no, l.status):
                entries.append({"date": l.date, "code": l.pl_no, "type": "Listing",
                                "party": "-", "desc": f"Listing · {l.status}",
                                "cin": 0, "cout": l.total, "link": "/listings"})
    if kind in ("", "Payroll"):
        for run in db.query(M.PayrollRun).filter(M.PayrollRun.status == "Confirmed").all():
            if match(run.month, "payroll", "salary"):
                try:
                    rd = run.run_date
                except Exception:
                    rd = date.today()
                if in_range(rd):
                    entries.append({"date": rd, "code": f"PAYROLL-{run.month}", "type": "Payroll",
                                    "party": f"{len(run.items)} staff", "desc": f"Payroll {run.month}",
                                    "cin": 0, "cout": run.total_cost, "link": f"/payroll/run/{run.id}",
                                    "match_key": ("Payroll", run.id)})

    matched_keys = {(l.matched_type, l.matched_id) for l in
                    db.query(M.BankStatementLine).filter(M.BankStatementLine.matched == True).all()}  # noqa: E712
    for e in entries:
        key = e.pop("match_key", None)
        e["reconciled"] = (key in matched_keys) if key else None

    entries.sort(key=lambda e: (e["date"], e["code"]), reverse=True)
    total_in = sum(e["cin"] for e in entries)
    total_out = sum(e["cout"] for e in entries)
    kinds = ["Payment", "Sale", "Petty Cash", "Voucher", "Listing", "Payroll"]
    return render(request, db, "general_ledger.html", "gl", entries=entries[:500],
                  q=q, frm=frm, to=to, kind=kind, kinds=kinds,
                  total_in=total_in, total_out=total_out, count=len(entries))


# ─────────────────────────── REPORTS ───────────────────────────
@app.get("/reports/ap-aging", response_class=HTMLResponse)
def ap_aging(request: Request, supplier: str = "", bucket: str = "", status: str = "",
             db: Session = Depends(get_db)):
    """Unpaid supplier payments grouped by supplier + age bucket, with filters."""
    today = date.today()
    q = db.query(M.Payment).filter(
        M.Payment.status.in_(["Unsorted", "Categorized", "On Voucher"]))
    if supplier:
        q = q.filter(func.lower(M.Payment.supplier) == supplier.strip().lower())
    if status:
        q = q.filter(M.Payment.status == status)
    open_pays = q.all()
    supplier_names = sorted({p.supplier or "(no supplier)" for p in
        db.query(M.Payment).filter(M.Payment.status.in_(["Unsorted", "Categorized", "On Voucher"])).all()})
    buckets = ["Current", "1-30", "31-60", "61-90", "90+"]
    if bucket:
        pass  # bucket filter applied below per-row
    rows = {}   # supplier -> {bucket: amount, total, items}
    for p in open_pays:
        age = (today - p.date).days
        b = ("Current" if age <= 0 else "1-30" if age <= 30 else "31-60" if age <= 60
             else "61-90" if age <= 90 else "90+")
        if bucket and b != bucket:
            continue
        name = p.supplier or "(no supplier)"
        r = rows.setdefault(name, {bk: 0.0 for bk in buckets})
        r.setdefault("total", 0.0)
        r.setdefault("items", [])
        r[b] += p.amount
        r["total"] += p.amount
        r["items"].append((p, b, age))
    totals = {bk: sum(r[bk] for r in rows.values()) for bk in buckets}
    grand = sum(totals.values())
    rows = dict(sorted(rows.items(), key=lambda kv: kv[1]["total"], reverse=True))
    return render(request, db, "ap_aging.html", "apaging",
                  rows=rows, buckets=buckets, totals=totals, grand=grand, today=today,
                  supplier_names=supplier_names, f_supplier=supplier, f_bucket=bucket,
                  f_status=status)


@app.get("/reports/statutory", response_class=HTMLResponse)
def statutory_report(request: Request, db: Session = Depends(get_db)):
    """Monthly EPF/SOCSO/EIS/PCB owed from confirmed payroll runs, with paid status."""
    from datetime import datetime as _dt
    runs = db.query(M.PayrollRun).filter(M.PayrollRun.status == "Confirmed").all()
    paid = {(s.month, s.kind): s for s in db.query(M.StatutoryPaid).all()}
    months = {}
    for run in runs:
        m = months.setdefault(run.month, {"EPF": 0.0, "SOCSO": 0.0, "EIS": 0.0, "PCB": 0.0})
        for it in run.items:
            m["EPF"] += it.epf_er + it.epf_ee
            m["SOCSO"] += it.socso_er + it.socso_ee
            m["EIS"] += it.eis_er + it.eis_ee
            m["PCB"] += it.pcb

    def due_date(month_str_):
        try:
            base = _dt.strptime(month_str_, "%b %Y")
            nxt = (base.month % 12) + 1
            yr = base.year + (1 if base.month == 12 else 0)
            return date(yr, nxt, 15)
        except Exception:
            return None

    report = []
    for mo, kinds in sorted(months.items(), key=lambda kv: due_date(kv[0]) or date.min):
        for kind, amt in kinds.items():
            if amt <= 0:
                continue
            rec = paid.get((mo, kind))
            report.append({"month": mo, "kind": kind, "amount": amt,
                           "due": due_date(mo), "paid": bool(rec),
                           "paid_date": rec.paid_date if rec else None,
                           "overdue": (not rec) and due_date(mo) and due_date(mo) < date.today()})
    total_owed = sum(r["amount"] for r in report if not r["paid"])
    return render(request, db, "statutory.html", "statutory",
                  report=report, total_owed=total_owed)


@app.post("/reports/statutory/pay")
def statutory_pay(request: Request, month: str = Form(...), kind: str = Form(...),
                  amount: float = Form(0), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    existing = db.query(M.StatutoryPaid).filter_by(month=month, kind=kind).first()
    if existing:
        db.delete(existing)   # toggle back to owed
    else:
        db.add(M.StatutoryPaid(month=month, kind=kind, amount=amount,
                               paid_date=date.today(), paid_by=user.display_name))
    db.commit()
    return RedirectResponse("/reports/statutory", status_code=302)


@app.get("/reports/tax", response_class=HTMLResponse)
def tax_report(request: Request, month: str = "", db: Session = Depends(get_db)):
    mo = month or month_str()
    months = sorted({m for (m,) in db.query(M.Payment.month).distinct() if m}
                    | {m for (m,) in db.query(M.SalesEntry.month).distinct() if m}
                    | {month_str()})
    out_tax = db.query(M.SalesEntry).filter(M.SalesEntry.month == mo,
                                            M.SalesEntry.tax_amount > 0).all()
    in_tax = db.query(M.Payment).filter(M.Payment.month == mo,
                                        M.Payment.tax_amount > 0,
                                        M.Payment.status != "Void").all()
    total_out = sum(s.tax_amount for s in out_tax)
    total_in = sum(p.tax_amount for p in in_tax)
    settings = {s.key: s.value for s in db.query(M.Setting).all()}
    return render(request, db, "tax.html", "tax", month=mo, months=months,
                  out_tax=out_tax, in_tax=in_tax, total_out=total_out, total_in=total_in,
                  net_tax=total_out - total_in,
                  sst_registered=settings.get("SST_REGISTERED", "no") == "yes",
                  sst_no=settings.get("SST_NUMBER", ""))


# ─────────────────────────── CSV EXPORTS ───────────────────────────
def _csv_response(filename: str, header: list, rows: list):
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    from fastapi.responses import Response
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/export/payments.csv")
def export_payments(request: Request, db: Session = Depends(get_db)):
    if not current_user(request, db):
        return RedirectResponse("/login", status_code=302)
    rows = [[p.pay_no, p.date, p.supplier, p.invoice_no, p.description, p.category, p.grp,
             p.amount, p.tax_type, p.tax_amount, p.month, p.status,
             p.voucher.pv_no if p.voucher else ""]
            for p in db.query(M.Payment).order_by(M.Payment.id).all()]
    return _csv_response("payments.csv",
        ["Payment No", "Date", "Supplier", "Invoice No", "Description", "Category", "Group",
         "Amount", "Tax Type", "Tax Amount", "Month", "Status", "Voucher"], rows)


@app.get("/export/sales.csv")
def export_sales(request: Request, db: Session = Depends(get_db)):
    if not current_user(request, db):
        return RedirectResponse("/login", status_code=302)
    rows = [[s.date, s.stream, s.description, s.amount, s.tax_type, s.tax_amount,
             s.method, s.month, s.recorded_by]
            for s in db.query(M.SalesEntry).order_by(M.SalesEntry.id).all()]
    return _csv_response("sales.csv",
        ["Date", "Stream", "Description", "Amount", "Tax Type", "Tax Amount",
         "Method", "Month", "Recorded By"], rows)


@app.get("/export/pettycash.csv")
def export_pettycash(request: Request, db: Session = Depends(get_db)):
    if not current_user(request, db):
        return RedirectResponse("/login", status_code=302)
    entries = db.query(M.PettyCashEntry).order_by(M.PettyCashEntry.date, M.PettyCashEntry.id).all()
    rows, bal = [], 0.0
    for e in entries:
        bal += e.amount_in - e.amount_out
        rows.append([e.date, e.description, e.category, e.amount_out, e.amount_in, bal, e.recorded_by])
    return _csv_response("petty_cash.csv",
        ["Date", "Description", "Category", "Out", "In", "Balance", "Recorded By"], rows)


# ─────────────────────────── P&L ───────────────────────────
@app.get("/pnl", response_class=HTMLResponse)
def pnl(request: Request, month: str = "", db: Session = Depends(get_db)):
    mo = month or month_str()
    months = sorted({m for (m,) in db.query(M.SalesEntry.month).distinct().all() if m}
                    | {m for (m,) in db.query(M.Payment.month).distinct().all() if m}
                    | {month_str()})

    # Revenue
    revenue = dict(db.query(M.SalesEntry.stream, func.sum(M.SalesEntry.amount))
                   .filter(M.SalesEntry.month == mo).group_by(M.SalesEntry.stream).all())
    total_rev = sum(revenue.values())

    # Payments in month, by group
    pays = db.query(M.Payment).filter(M.Payment.month == mo,
                                      M.Payment.status != "Void").all()
    def by_cat(group):
        out = {}
        for p in pays:
            if p.grp == group:
                out.setdefault(p.category or "Uncategorized", []).append(p)
        return out

    cogs_raw = by_cat("COGS")
    opex_raw = by_cat("OPEX")
    capex_raw = by_cat("CAPEX")
    other_raw = {}
    for p in pays:
        if p.grp not in ("COGS", "OPEX", "CAPEX", "Payroll"):
            other_raw.setdefault(p.category or "Uncategorized", []).append(p)

    # Petty cash spend rolls into the SAME categories as supplier purchases —
    # otherwise identical cat-food spend shows in a different P&L bucket
    # depending on whether it was paid by invoice or petty cash.
    from types import SimpleNamespace
    for e in db.query(M.PettyCashEntry).filter(M.PettyCashEntry.month == mo,
                                               M.PettyCashEntry.amount_out > 0).all():
        cat = e.category or "Uncategorized"
        grp = M.group_for(cat)
        row = SimpleNamespace(pay_no="PC", supplier=e.recorded_by,
                              description=f"Petty cash: {e.description}", amount=e.amount_out)
        target = cogs_raw if grp == "COGS" else capex_raw if grp == "CAPEX" else opex_raw
        target.setdefault(cat, []).append(row)

    def finalize(raw):
        return {k: (sum(x.amount for x in v), v) for k, v in sorted(raw.items())}

    cogs, opex, capex, other = finalize(cogs_raw), finalize(opex_raw), finalize(capex_raw), finalize(other_raw)
    total_cogs = sum(a for a, _ in cogs.values())
    total_opex = sum(a for a, _ in opex.values())
    total_capex = sum(a for a, _ in capex.values())
    total_other = sum(a for a, _ in other.values())

    # Payroll: confirmed runs for the month (employer cost)
    payroll_total = db.query(func.coalesce(func.sum(M.PayrollRun.total_cost), 0)) \
        .filter(M.PayrollRun.month == mo, M.PayrollRun.status == "Confirmed").scalar()

    gross_profit = total_rev - total_cogs
    total_operating = total_opex + total_other + payroll_total
    net = gross_profit - total_operating

    return render(request, db, "pnl.html", "pnl", month=mo, months=months,
                  revenue=revenue, total_rev=total_rev,
                  cogs=cogs, total_cogs=total_cogs, gross_profit=gross_profit,
                  opex=opex, total_opex=total_opex, other=other, total_other=total_other,
                  payroll_total=payroll_total,
                  total_operating=total_operating, net=net,
                  capex=capex, total_capex=total_capex)


# ─────────────────────────── SETTINGS ───────────────────────────
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    users = db.query(M.User).order_by(M.User.id).all()
    settings = {s.key: s.value for s in db.query(M.Setting).all()}
    return render(request, db, "settings.html", "settings",
                  users=users, settings=settings,
                  bot_configured=bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
                  ai_configured=bool(os.environ.get("ANTHROPIC_API_KEY")))


@app.post("/settings/users/new")
def user_new(request: Request, username: str = Form(...), password: str = Form(...),
             display_name: str = Form(...), role: str = Form("staff"),
             telegram_id: str = Form(""), db: Session = Depends(get_db)):
    me = current_user(request, db)
    if not me or me.role != "admin":
        return RedirectResponse("/", status_code=302)
    db.add(M.User(username=username.strip().lower(), password_hash=hash_password(password),
                  display_name=display_name, role=role, telegram_id=telegram_id.strip()))
    db.commit()
    return RedirectResponse("/settings", status_code=302)


@app.post("/settings/users/{uid}/toggle")
def user_toggle(uid: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request, db)
    if not me or me.role != "admin":
        return RedirectResponse("/", status_code=302)
    u = db.get(M.User, uid)
    if u and u.id != me.id:
        u.active = not u.active
        db.commit()
    return RedirectResponse("/settings", status_code=302)


@app.post("/settings/users/{uid}/password")
def user_password(uid: int, request: Request, password: str = Form(...),
                  db: Session = Depends(get_db)):
    me = current_user(request, db)
    if not me or (me.role != "admin" and me.id != uid):
        return RedirectResponse("/", status_code=302)
    u = db.get(M.User, uid)
    if u:
        u.password_hash = hash_password(password)
        db.commit()
    return RedirectResponse("/settings", status_code=302)


@app.post("/settings/save")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    me = current_user(request, db)
    if not me or me.role != "admin":
        return RedirectResponse("/", status_code=302)
    form = await request.form()
    for key in ("COMPANY_NAME", "COMPANY_ADDRESS", "TELEGRAM_WHITELIST", "PETTY_CASH_FLOAT",
                "PASSCODE", "SST_REGISTERED", "SST_NUMBER", "COMPANY_ROC", "COMPANY_BANK",
                "COMPANY_BANK_ACCOUNT", "COMPANY_TIN", "COMPANY_MSIC",
                "PREFIX_DOC", "PREFIX_PAY", "PREFIX_PV", "PREFIX_PL"):
        if key in form:
            s = db.get(M.Setting, key)
            if not s:
                s = M.Setting(key=key)
                db.add(s)
            s.value = str(form[key])
    db.commit()
    return RedirectResponse("/settings", status_code=302)


# ─────────────────────────── TELEGRAM WEBHOOK ───────────────────────────
@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request, db: Session = Depends(get_db)):
    if secret not in (WEBHOOK_SECRET, WEBHOOK_TOKEN):
        raise HTTPException(403)
    update = await request.json()
    try:
        telegram_bot.handle_update(update, db)
    except Exception as e:
        print("Telegram error:", e)
    return PlainTextResponse("ok")


@app.get("/health")
def health():
    return {"status": "ok", "app": "CATDAY System"}
