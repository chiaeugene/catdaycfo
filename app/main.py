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
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "catdayhook")
BASE_URL = os.environ.get("BASE_URL", "https://catday-system.onrender.com").rstrip("/")
# URL-safe token derived from the secret — base64 secrets contain +/= which
# break URL path segments, so the webhook path uses this hex digest instead.
import hashlib as _hashlib
WEBHOOK_TOKEN = _hashlib.sha256(WEBHOOK_SECRET.encode()).hexdigest()[:40]

NAV = [
    ("dashboard", "/", "home", "Dashboard", ("admin", "manager", "staff")),
    ("documents", "/documents", "inbox", "Verification", ("admin", "manager")),
    ("payments", "/payments", "card", "Payments", ("admin", "manager")),
    ("suppliers", "/suppliers", "landmark", "Suppliers", ("admin", "manager")),
    ("vouchers", "/vouchers", "receipt", "Vouchers", ("admin", "manager")),
    ("listings", "/listings", "list", "Listings", ("admin", "manager")),
    ("pettycash", "/pettycash", "coins", "Petty Cash", ("admin", "manager", "staff")),
    ("sales", "/sales", "cart", "Sales", ("admin", "manager", "staff")),
    ("payroll", "/payroll", "banknote", "Payroll", ("admin",)),
    ("pnl", "/pnl", "chart", "P&L Report", ("admin",)),
    ("settings", "/settings", "settings", "Settings", ("admin",)),
]


def render(request: Request, db: Session, template: str, page: str, **ctx):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    allowed = next((roles for key, _, _, _, roles in NAV if key == page), ())
    if user.role not in allowed:
        return RedirectResponse("/", status_code=302)
    nav = [(url, icon, label) for key, url, icon, label, roles in NAV if user.role in roles]
    pending_docs = db.query(M.Document).filter(M.Document.status == "Pending").count() \
        if user.role in ("admin", "manager") else 0
    return templates.TemplateResponse(request, template,
        {"user": user, "nav": nav, "page": page, "M": M, "today": date.today(),
         "pending_docs": pending_docs, **ctx})


def month_str(d: date | None = None) -> str:
    return f"{d or date.today():%b %Y}"


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date() if s else date.today()


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
    pending = db.query(M.Document).filter(M.Document.status == "Pending") \
        .order_by(M.Document.id).all()
    q = db.query(M.Document).filter(M.Document.status != "Pending") \
        .order_by(M.Document.id.desc())
    processed = q.limit(200).all()
    return render(request, db, "documents.html", "documents",
                  pending=pending, processed=processed, view=view)


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
def verify_document(doc_id: int, request: Request,
                    section: str = Form(...), doc_type: str = Form(...),
                    supplier: str = Form(""), amount: float = Form(0),
                    month: str = Form(""), description: str = Form(""),
                    category: str = Form(""), invoice_no: str = Form(""),
                    db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    doc = db.get(M.Document, doc_id)
    if not doc or doc.status != "Pending":
        return RedirectResponse("/documents", status_code=302)

    doc.section, doc.doc_type, doc.supplier = section, doc_type, supplier
    doc.amount, doc.month = amount, month or month_str()
    doc.description, doc.category = description, category
    doc.invoice_no = invoice_no.strip()
    doc.status, doc.verified_by, doc.verified_at = "Verified", user.display_name, datetime.utcnow()

    # Route to the right module
    if section in ("Purchase", "Expense", "Staff Claim"):
        pay_no = telegram_bot.next_counter(db, "PAY", "PAY-")
        if section == "Staff Claim":
            grp, category = "OPEX", "Staff Claim"
            # For claims the "supplier" is the claimant staff member being reimbursed
            supplier = supplier or doc.sender
        else:
            grp = "CAPEX" if section == "Purchase" else "OPEX"
        p = M.Payment(pay_no=pay_no, supplier=supplier, description=description,
                      category=category, grp=grp, amount=amount, month=doc.month,
                      invoice_no=invoice_no.strip(),
                      status="Categorized" if category else "Unsorted",
                      notes=f"from {doc.doc_no} ({doc.sender})")
        db.add(p)
        db.flush()
        doc.payment_id = p.id
    elif section == "Petty Cash":
        db.add(M.PettyCashEntry(date=date.today(), description=description or doc.doc_no,
                                category=category, amount_out=amount, month=doc.month,
                                recorded_by=user.display_name, document_id=doc.id))
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
                invoice_no: str = Form(""), pdate: str = Form(""), db: Session = Depends(get_db)):
    d = parse_date(pdate)
    pay_no = telegram_bot.next_counter(db, "PAY", "PAY-")
    db.add(M.Payment(pay_no=pay_no, date=d, supplier=supplier, description=description,
                     category=category, grp=grp, amount=amount, month=month_str(d),
                     invoice_no=invoice_no.strip(),
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


@app.post("/suppliers/new")
def supplier_new(request: Request, name: str = Form(...), sup_type: str = Form("Supplier"),
                 bank_name: str = Form(""), account_no: str = Form(""),
                 account_holder: str = Form(""), contact_person: str = Form(""),
                 phone: str = Form(""), email: str = Form(""), notes: str = Form(""),
                 db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    if not db.query(M.Supplier).filter(func.lower(M.Supplier.name) == name.strip().lower()).first():
        db.add(M.Supplier(name=name.strip(), sup_type=sup_type, bank_name=bank_name.strip(),
                          account_no=account_no.strip(), account_holder=account_holder.strip(),
                          contact_person=contact_person, phone=phone, email=email, notes=notes))
        db.commit()
    return RedirectResponse("/suppliers", status_code=302)


@app.post("/suppliers/{sid}/update")
def supplier_update(sid: int, request: Request, name: str = Form(...), sup_type: str = Form("Supplier"),
                    bank_name: str = Form(""), account_no: str = Form(""),
                    account_holder: str = Form(""), contact_person: str = Form(""),
                    phone: str = Form(""), email: str = Form(""), notes: str = Form(""),
                    active: str = Form(""), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    s = db.get(M.Supplier, sid)
    if s:
        s.name, s.sup_type = name.strip(), sup_type
        s.bank_name, s.account_no, s.account_holder = bank_name.strip(), account_no.strip(), account_holder.strip()
        s.contact_person, s.phone, s.email, s.notes = contact_person, phone, email, notes
        s.active = active == "on"
        db.commit()
    return RedirectResponse("/suppliers", status_code=302)


# ─────────────────────────── VOUCHERS ───────────────────────────
@app.get("/vouchers", response_class=HTMLResponse)
def vouchers(request: Request, db: Session = Depends(get_db)):
    pvs = db.query(M.Voucher).order_by(M.Voucher.id.desc()).limit(200).all()
    banks = supplier_map(db, [v.payee for v in pvs])
    return render(request, db, "vouchers.html", "vouchers", vouchers=pvs, banks=banks)


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
def listings(request: Request, db: Session = Depends(get_db)):
    pls = db.query(M.Listing).order_by(M.Listing.id.desc()).limit(200).all()
    names = [v.payee for pl in pls for v in pl.vouchers]
    banks = supplier_map(db, names)
    return render(request, db, "listings.html", "listings", listings=pls, banks=banks)


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


# ─────────────────────────── PETTY CASH ───────────────────────────
@app.get("/pettycash", response_class=HTMLResponse)
def pettycash(request: Request, month: str = "", db: Session = Depends(get_db)):
    entries = db.query(M.PettyCashEntry).order_by(M.PettyCashEntry.date, M.PettyCashEntry.id).all()
    bal = 0.0
    rows = []
    for e in entries:
        bal += e.amount_in - e.amount_out
        rows.append((e, bal))
    mo = month or month_str()
    month_rows = [(e, b) for e, b in rows if e.month == mo] if month else rows
    months = sorted({e.month for e in entries if e.month} | {month_str()})
    settings = {s.key: s.value for s in db.query(M.Setting).all()}
    float_target = float(settings.get("PETTY_CASH_FLOAT", "5000") or 5000)
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
                  mo_out=mo_out, mo_in=mo_in, by_cat=by_cat)


@app.post("/pettycash/new")
def pettycash_new(request: Request, description: str = Form(...), category: str = Form(""),
                  amount_out: float = Form(0), amount_in: float = Form(0),
                  pdate: str = Form(""), db: Session = Depends(get_db)):
    user = current_user(request, db)
    d = parse_date(pdate)
    db.add(M.PettyCashEntry(date=d, description=description, category=category,
                            amount_out=amount_out or 0, amount_in=amount_in or 0,
                            month=month_str(d),
                            recorded_by=user.display_name if user else ""))
    db.commit()
    return RedirectResponse("/pettycash", status_code=302)


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
              pdate: str = Form(""), db: Session = Depends(get_db)):
    user = current_user(request, db)
    d = parse_date(pdate)
    db.add(M.SalesEntry(date=d, stream=stream, description=description, amount=amount,
                        method=method, month=month_str(d),
                        recorded_by=user.display_name if user else ""))
    db.commit()
    return RedirectResponse("/sales", status_code=302)


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
                        overtime: float = Form(0), bonus: float = Form(0),
                        deductions: float = Form(0),
                        remarks: str = Form(""), db: Session = Depends(get_db)):
    run = db.get(M.PayrollRun, rid)
    item = db.get(M.PayrollItem, iid)
    if run and item and item.run_id == rid and run.status == "Draft":
        item.base, item.allowance, item.overtime, item.bonus = base, allowance, overtime, bonus
        item.deductions, item.remarks = deductions, remarks
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
        return {k: (sum(x.amount for x in v), v) for k, v in sorted(out.items())}

    cogs = by_cat("COGS")
    opex = by_cat("OPEX")
    capex = by_cat("CAPEX")
    other = {}
    for p in pays:
        if p.grp not in ("COGS", "OPEX", "CAPEX", "Payroll"):
            other.setdefault(p.category or "Uncategorized", []).append(p)
    other = {k: (sum(x.amount for x in v), v) for k, v in sorted(other.items())}

    total_cogs = sum(a for a, _ in cogs.values())
    total_opex = sum(a for a, _ in opex.values())
    total_capex = sum(a for a, _ in capex.values())
    total_other = sum(a for a, _ in other.values())

    # Payroll: confirmed runs for the month (employer cost)
    payroll_total = db.query(func.coalesce(func.sum(M.PayrollRun.total_cost), 0)) \
        .filter(M.PayrollRun.month == mo, M.PayrollRun.status == "Confirmed").scalar()

    # Petty cash usage in month
    petty_out = db.query(func.coalesce(func.sum(M.PettyCashEntry.amount_out), 0)) \
        .filter(M.PettyCashEntry.month == mo).scalar()

    gross_profit = total_rev - total_cogs
    total_operating = total_opex + total_other + payroll_total + petty_out
    net = gross_profit - total_operating

    return render(request, db, "pnl.html", "pnl", month=mo, months=months,
                  revenue=revenue, total_rev=total_rev,
                  cogs=cogs, total_cogs=total_cogs, gross_profit=gross_profit,
                  opex=opex, total_opex=total_opex, other=other, total_other=total_other,
                  payroll_total=payroll_total, petty_out=petty_out,
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
    for key in ("COMPANY_NAME", "COMPANY_ADDRESS", "TELEGRAM_WHITELIST", "PETTY_CASH_FLOAT", "PASSCODE"):
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
