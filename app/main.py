import os
from datetime import date, datetime, timedelta

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
from . import telegram_bot, pdfgen, claude_ai, ledger, backup, audit
from .audit import AccessControlMiddleware
from .statutory import calc_statutory

Base.metadata.create_all(engine)
run_migrations()

app = FastAPI(title="CATDAY System")
# Registration order matters: Starlette makes the LAST-added middleware the
# OUTERMOST wrapper, so SessionMiddleware must be added after
# AccessControlMiddleware — otherwise AccessControlMiddleware runs before
# SessionMiddleware has parsed the session cookie, and request.session raises.
app.add_middleware(AccessControlMiddleware)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "catday-dev-secret"))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.filters["rm"] = lambda v: f"{(v or 0):,.2f}"
templates.env.filters["abs"] = lambda v: abs(v or 0)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# Cache-buster for /static assets. Browsers hold onto style.css hard, so a CSS
# change would otherwise not reach users until they force-refresh. Keyed to the
# deployed commit in production, to file mtime locally.
def _asset_version() -> str:
    commit = os.environ.get("RENDER_GIT_COMMIT")
    if commit:
        return commit[:10]
    try:
        css = os.path.join(BASE_DIR, "static", "style.css")
        js = os.path.join(BASE_DIR, "static", "app.js")
        return str(int(max(os.path.getmtime(css), os.path.getmtime(js))))
    except OSError:
        return "dev"


ASSET_V = _asset_version()


# Automatic daily snapshot. A background daemon thread rather than cron —
# Render's Starter plan has no scheduler, and this keeps backups working
# wherever the app runs. The per-deploy snapshot in seed.py covers restarts,
# which reset this timer.
def _start_backup_scheduler(interval_hours: int = 24) -> None:
    import threading

    def loop():
        import time
        while True:
            time.sleep(interval_hours * 3600)
            try:
                backup.make_snapshot("auto-daily")
            except Exception:
                pass    # never let a failed backup take the app down

    if backup.sqlite_file():
        threading.Thread(target=loop, daemon=True, name="backup-scheduler").start()


_start_backup_scheduler()

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "catdayhook")
BASE_URL = os.environ.get("BASE_URL", "https://catday-system.onrender.com").rstrip("/")
# URL-safe token derived from the secret — base64 secrets contain +/= which
# break URL path segments, so the webhook path uses this hex digest instead.
import hashlib as _hashlib
WEBHOOK_TOKEN = _hashlib.sha256(WEBHOOK_SECRET.encode()).hexdigest()[:40]

# Grouped navigation: (group label, [(key, url, icon, label, roles), ...])
# Grouped by WHEN you use it, not by accounting taxonomy — the person running
# this daily is an operator, not a bookkeeper. Everyday work sits at the top;
# the formal accounting screens collapse away until an accountant needs them.
# "viewer" (Jasmine's role) is granted read access to every business page —
# she's the boss, she views everything — but never Setup, which is system
# config rather than business data. Mutation is blocked centrally for viewer
# regardless of what's listed here (see AccessControlMiddleware); these role
# tuples only control page *visibility* in the nav and the render() gate.
NAV_GROUPS = [
    ("", [
        ("dashboard", "/", "home", "Dashboard", ("admin", "manager", "staff", "viewer")),
    ]),
    ("Every day 每天", [
        ("documents", "/documents", "inbox", "Verify Inbox", ("admin", "manager", "viewer")),
        ("sales", "/sales", "cart", "Sales", ("admin", "manager", "staff", "viewer")),
        ("pettycash", "/pettycash", "coins", "Petty Cash", ("admin", "manager", "staff", "viewer")),
        ("boarding", "/boarding", "cat", "Boarding", ("admin", "manager", "staff", "viewer")),
        ("stock", "/stock", "coins", "Stock & Usage 库存", ("admin", "manager", "staff", "viewer")),
    ]),
    ("Paying suppliers 付款", [
        ("payments", "/payments", "card", "Payments", ("admin", "manager", "viewer")),
        ("vouchers", "/vouchers", "receipt", "Vouchers", ("admin", "manager", "viewer")),
        ("listings", "/listings", "list", "Listings", ("admin", "manager", "viewer")),
        ("suppliers", "/suppliers", "landmark", "Suppliers", ("admin", "manager", "viewer")),
    ]),
    ("Every month 每月", [
        ("payroll", "/payroll", "banknote", "Payroll", ("admin", "viewer")),
        ("statutory", "/reports/statutory", "landmark", "Statutory Dues", ("admin", "viewer")),
        ("reconciliation", "/reconciliation", "banknote", "Bank Reconciliation", ("admin", "manager", "viewer")),
        ("receivables", "/receivables", "receipt", "Receivables 应收", ("admin", "manager", "viewer")),
        ("pnl", "/pnl", "chart", "Profit & Loss", ("admin", "viewer")),
    ]),
    ("Reports 报告", [
        ("cashflow", "/reports/cashflow", "chart", "Cash Flow", ("admin", "viewer")),
        ("expansion", "/reports/expansion-budget", "landmark", "Expansion Budget", ("admin", "viewer")),
        ("apaging", "/reports/ap-aging", "list", "AP Aging", ("admin", "manager", "viewer")),
        ("araging", "/reports/ar-aging", "list", "AR Aging", ("admin", "manager", "viewer")),
        ("gl", "/reports/gl", "list", "General Ledger", ("admin", "manager", "viewer")),
        ("salesledger", "/reports/sales-ledger", "cart", "Sales Ledger", ("admin", "manager", "viewer")),
        ("purchledger", "/reports/purchase-ledger", "card", "Purchase Ledger", ("admin", "manager", "viewer")),
        ("tax", "/reports/tax", "receipt", "SST / Tax", ("admin", "manager", "viewer")),
        ("einvoice", "/reports/einvoice-readiness", "receipt", "e-Invoice Readiness", ("admin", "viewer")),
        ("auditlog", "/reports/audit-log", "list", "Audit Log", ("admin", "viewer")),
    ]),
    ("Accounting 会计", [
        ("tb", "/reports/trial-balance", "list", "Trial Balance", ("admin", "viewer")),
        ("bs", "/reports/balance-sheet", "chart", "Balance Sheet", ("admin", "viewer")),
        ("journal", "/accounting/journal", "receipt", "Journal", ("admin", "viewer")),
        ("coa", "/accounting/coa", "settings", "Chart of Accounts", ("admin", "viewer")),
    ]),
    ("Setup", [
        ("settings", "/settings", "settings", "Settings", ("admin",)),
        ("backups", "/settings/backups", "save", "Backups", ("admin",)),
    ]),
]
# Groups that start collapsed — accountant territory, not daily operations.
COLLAPSED_GROUPS = {"Accounting 会计"}
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
        if user.role in ("admin", "manager", "viewer") else 0
    return templates.TemplateResponse(request, template,
        {"user": user, "nav_groups": nav_groups, "page": page, "M": M, "today": date.today(),
         "pending_docs": pending_docs, "collapsed_groups": COLLAPSED_GROUPS,
         "asset_v": ASSET_V, **ctx})


def month_str(d: date | None = None) -> str:
    return f"{d or date.today():%b %Y}"


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date() if s else date.today()


def month_options(back: int = 12, fwd: int = 1) -> list[str]:
    """Selectable months around today, newest first. Free-typing a month is the
    single easiest way to lose a record — a typo like 'July 2026' matches no
    report filter — so every month input picks from this list."""
    today = date.today()
    out = []
    for i in range(-fwd, back + 1):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        out.append(f"{date(y, m, 1):%b %Y}")
    return out


def _months_between(start: str, end: str) -> list[str]:
    """Every month from start to end inclusive, as 'Mon YYYY', oldest first."""
    try:
        a = datetime.strptime(start, "%b %Y")
        b = datetime.strptime(end, "%b %Y")
    except ValueError:
        return [end]
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{date(y, m, 1):%b %Y}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _accounts_start_month(db: Session) -> str:
    """When the accounts begin: the opening-balance entry if one exists,
    otherwise the earliest month that carries any transaction."""
    opening = db.query(M.JournalEntry).filter(
        M.JournalEntry.source_type == "Opening").order_by(M.JournalEntry.date).first()
    if opening:
        return f"{opening.date:%b %Y}"
    candidates = []
    for model in (M.SalesEntry, M.Payment, M.PettyCashEntry):
        d = db.query(func.min(model.date)).scalar()
        if d:
            candidates.append(d)
    return f"{min(candidates):%b %Y}" if candidates else month_str()


def normalize_month(raw: str, fallback: date | None = None) -> str:
    """Accept 'Jul 2026', 'July 2026', '2026-07', '07/2026' → 'Jul 2026'."""
    raw = (raw or "").strip()
    if not raw:
        return month_str(fallback)
    for fmt in ("%b %Y", "%B %Y", "%Y-%m", "%m/%Y", "%b-%Y", "%B-%Y"):
        try:
            return f"{datetime.strptime(raw, fmt):%b %Y}"
        except ValueError:
            continue
    return month_str(fallback)


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
def _login_ctx(error: str = ""):
    return {"error": error, "asset_v": ASSET_V}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", _login_ctx())


@app.post("/login")
def login(request: Request, passcode: str = Form(...), db: Session = Depends(get_db)):
    # Each person's passcode identifies them — no profile picker. Whichever
    # active user's passcode matches is who gets logged in.
    passcode = passcode.strip()
    matched = None
    if passcode:
        for user in db.query(M.User).filter(M.User.active == True).all():  # noqa: E712
            if verify_password(passcode, user.password_hash):
                matched = user
                break
    if not matched:
        return templates.TemplateResponse(request, "login.html",
                                          _login_ctx("Wrong passcode  密码错误"))
    request.session["uid"] = matched.id
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
    # Spend must match the P&L exactly or the two screens contradict each other:
    # exclude Void payments, and include petty-cash spend + confirmed payroll.
    pay_month = db.query(func.coalesce(func.sum(M.Payment.amount), 0)) \
        .filter(M.Payment.month == mo, M.Payment.status != "Void").scalar()
    petty_month = db.query(func.coalesce(func.sum(M.PettyCashEntry.amount_out), 0)) \
        .filter(M.PettyCashEntry.month == mo).scalar()
    payroll_month = db.query(func.coalesce(func.sum(M.PayrollRun.total_cost), 0)) \
        .filter(M.PayrollRun.month == mo, M.PayrollRun.status == "Confirmed").scalar()

    overdue_stat = 0
    paid_stat = {(s.month, s.kind) for s in db.query(M.StatutoryPaid).all()}
    for run in db.query(M.PayrollRun).filter(M.PayrollRun.status == "Confirmed").all():
        try:
            base = datetime.strptime(run.month, "%b %Y")
            due = date(base.year + (1 if base.month == 12 else 0), (base.month % 12) + 1, 15)
        except ValueError:
            continue
        if due < date.today():
            overdue_stat += sum(1 for k in ("EPF", "SOCSO", "EIS", "PCB")
                                if (run.month, k) not in paid_stat)

    stats = {
        "docs_pending": db.query(M.Document).filter(M.Document.status == "Pending").count(),
        "pay_open": db.query(M.Payment).filter(M.Payment.status.in_(["Unsorted", "Categorized"])).count(),
        "pv_draft": db.query(M.Voucher).filter(M.Voucher.status == "Draft").count(),
        "pv_approved": db.query(M.Voucher).filter(M.Voucher.status == "Approved").count(),
        "uncategorized": db.query(M.Payment).filter(M.Payment.status == "Unsorted").count(),
        "unmatched_bank": db.query(M.BankStatementLine)
            .filter(M.BankStatementLine.matched == False).count(),  # noqa: E712
        "overdue_stat": overdue_stat,
        "sales_month": db.query(func.coalesce(func.sum(M.SalesEntry.amount), 0))
            .filter(M.SalesEntry.month == mo).scalar(),
        "expenses_month": pay_month + petty_month + payroll_month,
        "spend_split": {"payments": pay_month, "petty": petty_month, "payroll": payroll_month},
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
    _ensure_default_pc_account(db)

    # Duplicate detection: Karen's real claim schedules contained RM4,967.40 of
    # exact duplicates — catching a resubmitted invoice BEFORE it posts is one
    # of the highest-value checks a bookkeeper does.
    dup_warnings: dict[int, list[str]] = {}
    for d in pending:
        warns = []
        sup = (d.supplier or "").strip().lower()
        inv = (d.invoice_no or "").strip().lower()
        if sup and inv:
            for p in db.query(M.Payment).filter(M.Payment.status != "Void").all():
                if (p.supplier or "").strip().lower() == sup and \
                   (p.invoice_no or "").strip().lower() == inv:
                    warns.append(f"{p.pay_no} already has this supplier + invoice number")
        for other in db.query(M.Document).filter(M.Document.id != d.id,
                                                 M.Document.status != "Rejected").all():
            osup = (other.supplier or "").strip().lower()
            oinv = (other.invoice_no or "").strip().lower()
            if sup and inv and osup == sup and oinv == inv:
                warns.append(f"{other.doc_no} ({other.status.lower()}) has the same supplier + invoice number")
            elif sup and osup == sup and d.amount and other.amount == d.amount \
                    and other.status == "Pending":
                warns.append(f"{other.doc_no} (also pending) — same supplier and same amount RM {d.amount:,.2f}")
        if warns:
            dup_warnings[d.id] = warns

    # Account maps for the live posting preview on each verify card
    acct_names = {code: name for code, name, _typ in M.COA_SEED}
    flash = request.session.pop("flash", None)

    return render(request, db, "documents.html", "documents",
                  pending=pending, processed=processed, view=view, payloads=payloads,
                  months=month_options(), dup_warnings=dup_warnings, flash=flash,
                  category_account=M.CATEGORY_ACCOUNT, acct_names=acct_names,
                  supplier_names=[s.name for s in db.query(M.Supplier)
                                  .filter(M.Supplier.active == True).order_by(M.Supplier.name).all()],  # noqa: E712
                  pc_accounts=db.query(M.PettyCashAccount)
                                .filter(M.PettyCashAccount.active == True)  # noqa: E712
                                .order_by(M.PettyCashAccount.id).all())


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
    tax_type = str(f.get("tax_type", "None"))
    # The invoice date is what the books need — not the day someone got round
    # to verifying it. Falls back to today only when the form leaves it blank.
    doc_date = parse_date(str(f.get("ddate", ""))) if f.get("ddate") else date.today()

    doc.section = section
    doc.doc_type = str(f.get("doc_type", doc.doc_type))
    doc.supplier, doc.amount, doc.month = supplier, amount, normalize_month(month)
    doc.description, doc.category, doc.invoice_no = description, category, invoice_no
    doc.doc_date = doc_date
    doc.status, doc.verified_by, doc.verified_at = "Verified", user.display_name, datetime.utcnow()

    # Route to the right module — and record exactly what happened, so the
    # verifier sees the full trail: record created, journal posted, reports hit.
    acct = lambda code: f"{code} {dict((c, n) for c, n, _ in M.COA_SEED).get(code, '')}"  # noqa: E731
    made: list[str] = []
    links: list[tuple[str, str]] = []

    if section in ("Purchase", "Expense", "Staff Claim"):
        pay_no = telegram_bot.next_counter(db, "PAY", "PAY-")
        if section == "Staff Claim":
            grp, category = "OPEX", "Staff Claim"
            supplier = supplier or doc.sender   # claimant is reimbursed
        else:
            grp = M.group_for(category, section)   # cat-hotel category → P&L group
        p = M.Payment(pay_no=pay_no, date=doc_date, supplier=supplier, description=description,
                      category=category, grp=grp, amount=amount, month=doc.month,
                      invoice_no=invoice_no,
                      tax_type=tax_type, tax_amount=tax_of(tax_type, amount),
                      status="Categorized" if category else "Unsorted",
                      notes=f"from {doc.doc_no} ({doc.sender})")
        db.add(p)
        db.flush()
        doc.payment_id = p.id
        # Unknown supplier → create the directory record now, so the voucher /
        # AP-aging / bank-details chain links instead of dangling on a string.
        if section != "Staff Claim" and supplier and not db.query(M.Supplier).filter(
                func.lower(M.Supplier.name) == supplier.lower()).first():
            db.add(M.Supplier(name=supplier,
                              sup_type="Supplier" if section == "Purchase" else "Service Provider",
                              notes=f"Auto-created from {doc.doc_no} — add bank details "
                                    f"before putting this supplier on a voucher."))
            made.append(f"New supplier record: {supplier} (add bank details in Suppliers)")
        dr = ledger._expense_account_code(category or "", "Purchase" if grp == "CAPEX" else "")
        made.insert(0, f"{pay_no} · RM {amount:,.2f} · {grp}"
                       + (f" · incl. {tax_type} RM {p.tax_amount:,.2f}" if p.tax_amount else ""))
        made.append(f"Journal: Dr {acct(dr)} / Cr {acct(M.ACC_AP)}")
        links += [(pay_no, f"/payments/{p.id}"),
                  ("P&L", f"/pnl?month={doc.month}"), ("AP Aging", "/reports/ap-aging"),
                  ("Trial Balance", "/reports/trial-balance")]
        if p.tax_amount:
            links.append(("SST report", "/reports/tax"))

    elif section == "Petty Cash":
        # Route to the chosen tin; fall back to the default account so the
        # entry can never end up orphaned with no account_id.
        acc_id = int(f.get("pc_account_id") or 0) or None
        if not acc_id:
            _ensure_default_pc_account(db)
            first = db.query(M.PettyCashAccount).order_by(M.PettyCashAccount.id).first()
            acc_id = first.id if first else None
        db.add(M.PettyCashEntry(account_id=acc_id, date=doc_date,
                                description=description or doc.doc_no,
                                category=category, amount_out=amount, month=doc.month,
                                recorded_by=user.display_name, document_id=doc.id))
        dr = ledger._expense_account_code(category or "")
        made.append(f"Petty cash spend · RM {amount:,.2f}")
        made.append(f"Journal: Dr {acct(dr)} / Cr {acct(M.ACC_PETTY)}")
        links += [("Petty Cash", f"/pettycash?month={doc.month}"),
                  ("P&L", f"/pnl?month={doc.month}")]

    elif section == "Sales Report":
        rdate = parse_date(str(f.get("rdate", "")))
        total = 0.0
        streams_hit = []
        for stream in M.STREAMS:
            val = float(f.get(f"sales_{stream}") or 0)
            if val:
                db.add(M.SalesEntry(date=rdate, stream=stream,
                                    description=f"Daily report ({doc.doc_no})",
                                    amount=val, method="Mixed", month=month_str(rdate),
                                    qty=float(f.get(f"qty_{stream}") or 0),
                                    recorded_by=doc.sender))
                total += val
                streams_hit.append(stream)
        doc.amount = total
        made.append(f"Sales · RM {total:,.2f} across {', '.join(streams_hit) or 'no streams'}")
        made.append(f"Journal: Dr {acct(M.ACC_BANK)} / Cr revenue accounts per stream")
        links += [("Sales", "/sales"), ("P&L", f"/pnl?month={month_str(rdate)}")]
        # A daily report can also carry the cats in/out figures. Post them as a
        # boarding log in the same click rather than making someone re-key them.
        cats_in = int(float(f.get("cats_in") or 0))
        cats_out = int(float(f.get("cats_out") or 0))
        cats_occ = int(float(f.get("cats_occ") or 0))
        if cats_in or cats_out or cats_occ:
            db.add(M.BoardingLog(date=rdate, checked_in=cats_in, checked_out=cats_out,
                                 occupancy=cats_occ, notes=f"From daily report {doc.doc_no}",
                                 recorded_by=doc.sender))
            made.append(f"Boarding log · in {cats_in} · out {cats_out} · in-house {cats_occ}")
            links.append(("Boarding", "/boarding"))

    elif section == "Boarding Log":
        db.add(M.BoardingLog(
            date=parse_date(str(f.get("rdate", ""))),
            checked_in=int(float(f.get("checked_in") or 0)),
            checked_out=int(float(f.get("checked_out") or 0)),
            occupancy=int(float(f.get("occupancy") or 0)),
            notes=description, recorded_by=doc.sender))
        made.append("Boarding log entry (operational — no financial posting)")
        links.append(("Boarding", "/boarding"))

    elif section == "Bank-in Slip":
        # A real accounting event, not just filing: the deposit moves cash into
        # the bank. Revenue was already recognised when the sale was recorded.
        import json as _json
        credit = str(f.get("bankin_credit") or M.ACC_CASH)
        credit = M.COA_RECODE.get(credit, credit)
        if credit not in (M.ACC_CASH, M.ACC_TNG_CLEARING):
            credit = M.ACC_CASH
        try:
            payload = _json.loads(doc.payload_json or "{}")
        except ValueError:
            payload = {}
        payload["bankin_credit"] = credit
        doc.payload_json = _json.dumps(payload)
        if amount > 0:
            made.append(f"Bank-in recorded · RM {amount:,.2f}")
            made.append(f"Journal: Dr {acct(M.ACC_BANK)} / Cr {acct(credit)}")
            links += [("Journal", "/accounting/journal"),
                      ("Bank Reconciliation", "/reconciliation")]
        else:
            made.append("Filed — amount is 0, so no journal entry was posted")

    else:
        # Payroll document / Filing Only → archive with the file, no posting.
        made.append("Filed for reference — no financial posting")

    db.commit()
    # Derive the journal immediately so GL/TB/BS are already up to date when
    # the verifier clicks through — not on the next accounting-page visit.
    ledger.sync_ledger(db)

    request.session["flash"] = {
        "doc": doc.doc_no, "section": section,
        "made": made, "links": links,
    }
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
    grp = grp or M.group_for(category)   # derive rather than ask twice
    db.add(M.Payment(pay_no=pay_no, date=d, supplier=supplier, description=description,
                     category=category, grp=grp, amount=amount, month=month_str(d),
                     invoice_no=invoice_no.strip(), tax_type=tax_type,
                     tax_amount=tax_of(tax_type, amount),
                     status="Categorized" if category else "Unsorted", notes="manual entry"))
    db.commit()
    return RedirectResponse("/payments", status_code=302)


@app.get("/payments/{pid}", response_class=HTMLResponse)
def payment_detail(pid: int, request: Request, db: Session = Depends(get_db)):
    """One payment, everything attached to it — so every report can deep-link here."""
    p = db.get(M.Payment, pid)
    if not p:
        return RedirectResponse("/payments", status_code=302)
    supplier = db.query(M.Supplier).filter(
        func.lower(M.Supplier.name) == (p.supplier or "").lower()).first()
    journal = db.query(M.JournalEntry).filter(
        M.JournalEntry.source_type == "Payment", M.JournalEntry.source_id == p.id).all()
    return render(request, db, "payment_detail.html", "payments",
                  p=p, supplier=supplier, journal=journal)


@app.get("/vouchers/{vid}", response_class=HTMLResponse)
def voucher_detail(vid: int, request: Request, db: Session = Depends(get_db)):
    v = db.get(M.Voucher, vid)
    if not v:
        return RedirectResponse("/vouchers", status_code=302)
    supplier = db.query(M.Supplier).filter(
        func.lower(M.Supplier.name) == (v.payee or "").lower()).first()
    journal = db.query(M.JournalEntry).filter(
        M.JournalEntry.source_type == "Voucher", M.JournalEntry.source_id == v.id).all()
    bank_line = db.query(M.BankStatementLine).filter(
        M.BankStatementLine.matched_type == "Voucher",
        M.BankStatementLine.matched_id == v.id).first()
    return render(request, db, "voucher_detail.html", "vouchers",
                  v=v, supplier=supplier, journal=journal, bank_line=bank_line)


@app.post("/payments/{pid}/update")
def update_payment(pid: int, request: Request, supplier: str = Form(""),
                   category: str = Form(""), grp: str = Form(""),
                   amount: float = Form(0), db: Session = Depends(get_db)):
    p = db.get(M.Payment, pid)
    if p and p.status in ("Unsorted", "Categorized"):
        p.supplier, p.category, p.amount = supplier, category, amount
        p.grp = grp or M.group_for(category)
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
                             reg_no=settings.get("COMPANY_ROC", ""),
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
    pl_no = telegram_bot.next_monthly_counter(db, "PL", "PL-")
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
                             address=settings.get("COMPANY_ADDRESS", "Uptown PJ"),
                             reg_no=settings.get("COMPANY_ROC", ""))
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
              qty: float = Form(0), db: Session = Depends(get_db)):
    user = current_user(request, db)
    d = parse_date(pdate)
    db.add(M.SalesEntry(date=d, stream=stream, description=description, amount=amount,
                        method=method, month=month_str(d), tax_type=tax_type,
                        tax_amount=tax_of(tax_type, amount), qty=qty or 0,
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
                               address=settings.get("COMPANY_ADDRESS", "Uptown PJ"),
                               reg_no=settings.get("COMPANY_ROC", ""))
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
                             address=settings.get("COMPANY_ADDRESS", "Uptown PJ"),
                             reg_no=settings.get("COMPANY_ROC", ""))
    full = os.path.join(UPLOAD_DIR, rel)
    return FileResponse(full, filename=os.path.basename(full),
                        content_disposition_type="inline")


# ─────────────────────────── BANK RECONCILIATION ───────────────────────────
def _auto_match_lines(db: Session, account_id: int, only_batch: str = "") -> dict:
    """Match unmatched statement lines against unmatched book transactions.

    Deliberately conservative — a wrong match silently corrupts the
    reconciliation, an unmatched line just waits for a human. A line is only
    auto-matched when the evidence is unambiguous:
      1. The candidate's code (PV-0001 / PAYROLL-...) or payee name appears in
         the bank narration AND the amounts agree to the sen, or
      2. Exactly ONE candidate has the same amount (to the sen, same
         direction) within ±5 days. Two candidates with the same amount →
         nobody gets matched; a human decides.
    Every auto-match is labelled in matched_note and reversible with Undo.
    """
    q = db.query(M.BankStatementLine).filter(
        M.BankStatementLine.bank_account_id == account_id,
        M.BankStatementLine.matched == False)  # noqa: E712
    if only_batch:
        q = q.filter(M.BankStatementLine.import_batch == only_batch)
    lines = q.all()
    pool = _unmatched_system_txns(db, account_id)
    stats = {"matched": 0, "left": 0}

    for line in lines:
        same_amount = [c for c in pool
                       if abs(c["amount"] - line.amount) < 0.01
                       and (c["amount"] > 0) == (line.amount > 0)]
        chosen, why = None, ""
        narration = f"{line.description} {line.ref}".lower()
        # Strongest signal: our reference or the payee's name in the narration
        for c in same_amount:
            code = str(c.get("desc", "")).split("·")[0].strip().lower()
            party = str(c.get("party", "")).strip().lower()
            if (code and len(code) >= 4 and code in narration) or \
               (party and len(party) >= 5 and party in narration):
                chosen, why = c, "reference/payee in narration"
                break
        # Otherwise: unique amount within a tight date window
        if not chosen:
            close = [c for c in same_amount if abs((c["date"] - line.date).days) <= 5]
            if len(close) == 1:
                chosen, why = close[0], "unique amount within 5 days"
        if chosen:
            line.matched, line.matched_type, line.matched_id = True, chosen["type"], chosen["id"]
            line.matched_note = f"auto: {why}"
            pool.remove(chosen)     # a book transaction can only explain one line
            stats["matched"] += 1
        else:
            stats["left"] += 1
    db.commit()
    return stats


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
                  balance_per_bank=balance_per_bank, balance_per_books=balance_per_books,
                  flash_recon=request.session.pop("flash_recon", None))


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
    # Try to match the fresh lines immediately — most of a clean statement
    # should reconcile itself, leaving only genuine mysteries for a human.
    stats = _auto_match_lines(db, account_id, only_batch=batch)
    request.session["flash_recon"] = (
        f"Imported {added} lines · auto-matched {stats['matched']}"
        + (f" · {stats['left']} need review" if stats["left"] else " · all reconciled ✓"))
    return RedirectResponse(f"/reconciliation?account={account_id}", status_code=302)


@app.post("/reconciliation/auto-match")
def reconciliation_auto_match(request: Request, account_id: int = Form(...),
                              db: Session = Depends(get_db)):
    """Run the matcher over ALL unmatched lines for this account — for lines
    imported before auto-matching existed, or after new book records land."""
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    stats = _auto_match_lines(db, account_id)
    request.session["flash_recon"] = (
        f"Auto-match: {stats['matched']} matched"
        + (f" · {stats['left']} still need review" if stats["left"] else " · nothing left ✓"))
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


# ─────────────────────────── DOUBLE-ENTRY ACCOUNTING ───────────────────────────
@app.get("/reports/trial-balance", response_class=HTMLResponse)
def trial_balance_page(request: Request, as_of: str = "", frm: str = "",
                       db: Session = Depends(get_db)):
    """As-of TB by default; give a From date to get the period view with
    opening balance, period movement, and closing balance columns."""
    d = parse_date(as_of) if as_of else date.today()
    d_from = parse_date(frm) if frm else None
    if d_from and d_from <= d:
        range_rows, range_tot = ledger.trial_balance_range(db, d_from, d)
        return render(request, db, "trial_balance.html", "tb",
                      rows=[], tot_dr=range_tot["close_dr"], tot_cr=range_tot["close_cr"],
                      as_of=d, frm=d_from, range_rows=range_rows, range_tot=range_tot)
    rows, tot_dr, tot_cr = ledger.trial_balance(db, d)
    return render(request, db, "trial_balance.html", "tb",
                  rows=rows, tot_dr=tot_dr, tot_cr=tot_cr, as_of=d,
                  frm=None, range_rows=None, range_tot=None)


@app.get("/reports/balance-sheet", response_class=HTMLResponse)
def balance_sheet_page(request: Request, as_of: str = "", vs: str = "",
                       db: Session = Depends(get_db)):
    """As-at position, with an optional comparison date (e.g. month-end vs
    opening) shown side by side."""
    d = parse_date(as_of) if as_of else date.today()
    bs = ledger.balance_sheet(db, d)
    d_vs = parse_date(vs) if vs else None
    bs_vs = ledger.balance_sheet(db, d_vs) if d_vs else None
    vs_bal = None
    if bs_vs:
        vs_bal = {acc.id: bal for acc, bal in
                  bs_vs["assets"] + bs_vs["liabilities"] + bs_vs["equity"]}
    has_opening = db.query(M.JournalEntry).filter(
        M.JournalEntry.source_type == "Opening").count() > 0
    return render(request, db, "balance_sheet.html", "bs", bs=bs, as_of=d,
                  vs_date=d_vs, bs_vs=bs_vs, vs_bal=vs_bal,
                  has_opening=has_opening)


@app.get("/accounting/journal", response_class=HTMLResponse)
def journal_page(request: Request, src: str = "", month: str = "",
                 frm: str = "", to: str = "", db: Session = Depends(get_db)):
    ledger.sync_ledger(db)
    q = db.query(M.JournalEntry)
    if src:
        q = q.filter(M.JournalEntry.source_type == src)
    if month:
        q = q.filter(M.JournalEntry.month == month)
    d_from, d_to = parse_date(frm) if frm else None, parse_date(to) if to else None
    if d_from:
        q = q.filter(M.JournalEntry.date >= d_from)
    if d_to:
        q = q.filter(M.JournalEntry.date <= d_to)
    entries = q.order_by(M.JournalEntry.date.desc(), M.JournalEntry.id.desc()).limit(300).all()
    months = sorted({m for (m,) in db.query(M.JournalEntry.month).distinct().all() if m})
    sources = [s for (s,) in db.query(M.JournalEntry.source_type).distinct().all()]
    accounts = db.query(M.Account).filter(M.Account.active == True).order_by(M.Account.code).all()  # noqa: E712
    return render(request, db, "journal.html", "journal", entries=entries,
                  months=months, sources=sources, src=src, month=month,
                  frm=frm, to=to, accounts=accounts)


@app.post("/accounting/journal/manual")
async def journal_manual(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    form = await request.form()
    entry_date = parse_date(form.get("date", "")) or date.today()
    memo = (form.get("memo") or "Manual journal").strip()
    lines = []
    for i in range(1, 7):
        aid = form.get(f"acc{i}")
        if not aid:
            continue
        try:
            lines.append((int(aid), float(form.get(f"dr{i}") or 0),
                          float(form.get(f"cr{i}") or 0), form.get(f"desc{i}") or ""))
        except ValueError:
            continue
    try:
        n = telegram_bot.next_counter(db, "MJE", "MJE-")
        ledger.post_manual(db, entry_date, memo, lines, user.display_name, ref=n)
    except ValueError:
        pass  # unbalanced/empty — silently ignored; page shows nothing posted
    return RedirectResponse("/accounting/journal", status_code=302)


@app.post("/accounting/journal/{jid}/delete")
def journal_delete(jid: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    je = db.get(M.JournalEntry, jid)
    if je and je.source_type in ("Manual", "Opening"):   # auto entries are derived — not deletable
        db.delete(je)
        db.commit()
    return RedirectResponse("/accounting/journal", status_code=302)


@app.post("/accounting/rebuild")
def ledger_rebuild(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    ledger.rebuild_ledger(db)
    return RedirectResponse("/accounting/journal", status_code=302)


@app.get("/accounting/ledger/{account_id}", response_class=HTMLResponse)
def account_ledger(account_id: int, request: Request, frm: str = "", to: str = "",
                   db: Session = Depends(get_db)):
    ledger.sync_ledger(db)
    acc = db.get(M.Account, account_id)
    if not acc:
        return RedirectResponse("/reports/trial-balance", status_code=302)
    d_from, d_to = parse_date(frm) if frm else None, parse_date(to) if to else None
    lines = db.query(M.JournalLine).join(M.JournalEntry).filter(
        M.JournalLine.account_id == account_id).order_by(
        M.JournalEntry.date, M.JournalEntry.id).all()
    # Running balance always starts from the true beginning; a From filter
    # shows the balance brought forward instead of silently restarting at 0.
    running = 0.0
    bf = 0.0
    rows = []
    for l in lines:
        running += l.debit - l.credit
        if d_from and l.entry.date < d_from:
            bf = running
            continue
        if d_to and l.entry.date > d_to:
            continue
        rows.append({"line": l, "entry": l.entry, "balance": round(running, 2)})
    return render(request, db, "account_ledger.html", "tb", acc=acc, rows=rows,
                  frm=frm, to=to, bf=round(bf, 2) if d_from else None)


@app.get("/accounting/coa", response_class=HTMLResponse)
def coa_page(request: Request, db: Session = Depends(get_db)):
    ledger.seed_coa(db)
    accounts = db.query(M.Account).order_by(M.Account.code).all()
    # balance-sheet accounts for the opening-balance form
    bs_accounts = [a for a in accounts if a.active and a.type in ("Asset", "Liability", "Equity")
                   and a.code != M.ACC_OBE]
    opening = db.query(M.JournalEntry).filter(M.JournalEntry.source_type == "Opening").first()
    return render(request, db, "chart_of_accounts.html", "coa",
                  accounts=accounts, bs_accounts=bs_accounts, opening=opening)


@app.post("/accounting/coa/new")
def coa_new(request: Request, code: str = Form(...), name: str = Form(...),
            acc_type: str = Form(...), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    code = code.strip()
    if code and name.strip() and acc_type in M.ACCOUNT_TYPES and \
            not db.query(M.Account).filter(M.Account.code == code).first():
        db.add(M.Account(code=code, name=name.strip(), type=acc_type))
        db.commit()
    return RedirectResponse("/accounting/coa", status_code=302)


@app.post("/accounting/coa/{aid}/toggle")
def coa_toggle(aid: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    acc = db.get(M.Account, aid)
    if acc and not acc.is_system:
        acc.active = not acc.active
        db.commit()
    return RedirectResponse("/accounting/coa", status_code=302)


@app.post("/accounting/opening")
async def opening_balances(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    form = await request.form()
    entry_date = parse_date(form.get("date", "")) or date.today()
    lines = []
    total_dr = total_cr = 0.0
    for acc in db.query(M.Account).filter(M.Account.active == True).all():  # noqa: E712
        raw = form.get(f"amount_{acc.id}")
        if not raw:
            continue
        try:
            amt = float(raw)
        except ValueError:
            continue
        if abs(amt) < 0.005:
            continue
        # Positive amount posts to the account's natural side; negative flips
        # (e.g. Accumulated Depreciation entered as negative under Assets).
        if acc.type == "Asset":
            dr, cr = (amt, 0) if amt > 0 else (0, -amt)
        else:
            dr, cr = (0, amt) if amt > 0 else (-amt, 0)
        lines.append((acc.id, dr, cr, "Opening balance"))
        total_dr += dr
        total_cr += cr
    diff = round(total_dr - total_cr, 2)
    if abs(diff) > 0.005:   # plug the difference to Opening Balance Equity
        obe = db.query(M.Account).filter(M.Account.code == M.ACC_OBE).first()
        if obe:
            lines.append((obe.id, 0 if diff > 0 else -diff, diff if diff > 0 else 0,
                          "Opening balance equity (plug)"))
    if lines:
        # replace any previous opening entry
        for je in db.query(M.JournalEntry).filter(M.JournalEntry.source_type == "Opening").all():
            db.delete(je)
        db.commit()
        try:
            ledger.post_manual(db, entry_date, "Opening balances", lines,
                               user.display_name, source_type="Opening", ref="OPENING")
        except ValueError:
            pass
    return RedirectResponse("/accounting/coa", status_code=302)


# ─────────────────────────── EXPANSION BUDGET ───────────────────────────
# Static figures transcribed from the "Expansion Budget" tab of Karen's
# reconstruction workbook (31 Jul 2026 basis). Kept as data here rather than
# re-derived, because these are evidence-backed working totals with specific
# caveats attached — recomputing them from our ledger would silently drop the
# provisional/unverified distinctions the workbook is careful to preserve.
EXPANSION_BUDGET = {
    "as_at": "31 Jul 2026",
    "capital_reported": 1_500_000.00,
    "spend_myr": 805_988.37,
    "spend_china_rm": 113_340.70,
    "spend_total": 919_329.07,
    "opex_bank_paid": 150_007.95,   # v3: excludes RM519.27 project insurance, now CAPEX
    "outflow_known": 1_069_337.02,
    "funding_unmatched": 430_662.98,
    "claims_candidate": 152_406.84,
    "spend_detail": [
        ("TNJ base renovation", 696_129.20, "Supplier acknowledged"),
        ("TNJ variation works", 80_842.40, "Supplier acknowledged"),
        ("Project insurance", 519.27, "Confirmed"),
        ("Ventilation (Flow Elite)", 8_900.00, "Partially paid"),
        ("Membrane ceiling (Alwayz)", 9_761.75, "Confirmed"),
        ("China cat-house shipping (Big Tree)", 2_638.95, "Bank paid"),
        ("Medklinn equipment", 7_196.80, "Bank paid; invoice open"),
        ("China custom cat accommodation (CNY88,000)", 53_336.80, "Paid evidence"),
        ("China fresh-air / sterilisation (CNY66,000)", 40_002.60, "Paid evidence"),
        ("China amusement tunnel (CNY33,000)", 20_001.30, "Paid evidence"),
    ],
    "remaining_bills": [
        ("TNJ base renovation — not yet invoiced", 77_347.80, "Needs confirmation"),
        ("TNJ variation works", 36_533.70, "Needs confirmation"),
        ("Flow Elite ventilation", 4_700.00, "Invoice balance"),
        ("Winston air-conditioning", 6_000.00, "Payment unverified"),
        ("QQT freight", 5_204.98, "Payment unverified"),
        ("Big Tree cat-house shipping", 29_498.55, "Payment/entity check"),
    ],
    "remaining_total": 159_285.03,
    "monthly_budget": [
        ("Rent", 17_000.00, "Signed tenancy"),
        ("Salaries", 20_800.00, "Operator model"),
        ("Employer EPF / SOCSO / EIS", 3_000.00, "Review allowance — missing from operator model"),
        ("Cleaning supplies", 850.00, "Operator model"),
        ("Grooming supplies", 1_200.00, "Operator model"),
        ("Utilities", 4_000.00, "Operator model"),
        ("Food & litter", 4_800.00, "Operator model"),
        ("Marketing", 5_500.00, "Operator model"),
        ("Vet visits", 1_680.00, "Operator steady month"),
        ("Maintenance", 1_000.00, "Operator model"),
        ("Systems, administration & general allowance", 5_500.00,
         "Confirmed monthly management fee — incl. RM2,500 software administration"),
    ],
    "monthly_total": 65_330.00,
    "reserve_3m": 195_990.00,
    "reserve_6m": 391_980.00,
    "preopening": [
        ("Licensing, compliance, fire safety & insurance", 10_000.00),
        ("Opening consumables", 15_000.00),
        ("IT, POS, CCTV, booking & access control", 20_000.00),
        ("Launch marketing, signage & photography", 25_000.00),
        ("Defects, snagging & contingency", 40_000.00),
    ],
    "preopening_total": 110_000.00,
    "cash_need_base": 465_275.03,
    "cash_need_max": 546_458.81,
    "conditional": [
        ("SS21 tenancy security deposit (refundable)", 51_000.00,
         "Signed lease; payment proof still required"),
        ("Grooming equipment (CNY49,800)", 30_183.78,
         "Supplier chat only — no final invoice or payment proof"),
    ],
}


@app.get("/reports/expansion-budget", response_class=HTMLResponse)
def expansion_budget(request: Request, db: Session = Depends(get_db)):
    b = EXPANSION_BUDGET
    # Live comparison: what the ledger has actually recorded against the plan.
    ledger.sync_ledger(db)
    reno = db.query(M.Account).filter(M.Account.code == M.ACC_RENOVATION).first()
    recorded = 0.0
    if reno:
        for line in db.query(M.JournalLine).filter(M.JournalLine.account_id == reno.id).all():
            recorded += line.debit - line.credit
    return render(request, db, "expansion_budget.html", "expansion",
                  b=b, recorded_in_ledger=round(recorded, 2))


# ─────────────────────────── CASH FLOW PROJECTION ───────────────────────────
@app.get("/reports/cashflow", response_class=HTMLResponse)
def cashflow(request: Request, db: Session = Depends(get_db)):
    """Weekly cash projection: opening cash → expected collections → upcoming
    payments → projected closing, per week. Derived from the ledger, unpaid
    vouchers, statutory due dates, staff presets and a sales run-rate."""
    from datetime import datetime as _dt, timedelta

    ledger.sync_ledger(db)
    today = date.today()
    settings = {s.key: s.value for s in db.query(M.Setting).all()}

    def _setting_float(key):
        try:
            return float(settings.get(key, "") or 0) or None
        except ValueError:
            return None

    n_weeks = int(_setting_float("CF_WEEKS") or 8)
    n_weeks = max(2, min(16, n_weeks))
    horizon_end = today + timedelta(days=7 * n_weeks)

    # Opening cash: ledger balances of cash/bank/petty accounts as of today
    cash_codes = (M.ACC_CASH, M.ACC_BANK, M.ACC_PETTY, M.ACC_TNG_CLEARING)
    opening_cash = 0.0
    cash_split = []
    for acc in db.query(M.Account).filter(M.Account.code.in_(cash_codes)).all():
        bal = 0.0
        for line in db.query(M.JournalLine).join(M.JournalEntry).filter(
                M.JournalLine.account_id == acc.id, M.JournalEntry.date <= today).all():
            bal += line.debit - line.credit
        opening_cash += bal
        cash_split.append((acc, round(bal, 2)))

    # Inflow: weekly sales estimate — override or trailing-14-day run-rate
    since = today - timedelta(days=14)
    recent_sales = db.query(func.coalesce(func.sum(M.SalesEntry.amount), 0)) \
        .filter(M.SalesEntry.date >= since, M.SalesEntry.date <= today).scalar() or 0
    auto_weekly_sales = round(recent_sales / 14 * 7, 2)
    weekly_sales = _setting_float("CF_WEEKLY_SALES") or auto_weekly_sales

    events = []   # (date, label, amount) — outflows, positive amounts

    # 1) Approved but unpaid vouchers — assume paid within a few days
    for v in db.query(M.Voucher).filter(M.Voucher.status == "Approved").all():
        events.append((max(today, v.date + timedelta(days=3)),
                       f"{v.pv_no} · {v.payee}", v.total))

    # 2) Unpaid statutory from confirmed payroll — due 15th of following month
    paid_stat = {(s.month, s.kind) for s in db.query(M.StatutoryPaid).all()}
    stat_months = {}
    for run in db.query(M.PayrollRun).filter(M.PayrollRun.status == "Confirmed").all():
        m = stat_months.setdefault(run.month, {"EPF": 0.0, "SOCSO": 0.0, "EIS": 0.0, "PCB": 0.0})
        for it in run.items:
            m["EPF"] += it.epf_er + it.epf_ee
            m["SOCSO"] += it.socso_er + it.socso_ee
            m["EIS"] += it.eis_er + it.eis_ee
            m["PCB"] += it.pcb
    for mo, kinds in stat_months.items():
        try:
            base = _dt.strptime(mo, "%b %Y")
            due = date(base.year + (1 if base.month == 12 else 0), (base.month % 12) + 1, 15)
        except ValueError:
            continue
        for kind, amt in kinds.items():
            if amt > 0 and (mo, kind) not in paid_stat and due <= horizon_end:
                events.append((max(today, due), f"{kind} {mo} (statutory)", amt))

    # 3) Projected payroll for months without a confirmed run (from staff presets)
    staff = db.query(M.Staff).filter(M.Staff.active == True).all()  # noqa: E712
    preset_net = sum(s.net_pay for s in staff)
    preset_stat = sum(s.epf_employer + s.epf_employee + s.socso_employer + s.socso_employee
                      + s.eis_employer + s.eis_employee for s in staff)
    confirmed_months = {r.month for r in db.query(M.PayrollRun)
                        .filter(M.PayrollRun.status == "Confirmed").all()}
    cur = date(today.year, today.month, 1)
    while cur <= horizon_end:
        nxt_month = date(cur.year + (1 if cur.month == 12 else 0), (cur.month % 12) + 1, 1)
        month_end = nxt_month - timedelta(days=1)
        mo_str = f"{cur:%b %Y}"
        if mo_str not in confirmed_months and preset_net > 0 and month_end <= horizon_end:
            events.append((max(today, month_end), f"Payroll {mo_str} (projected net)", preset_net))
            stat_due = date(nxt_month.year, nxt_month.month, 15)
            if stat_due <= horizon_end:
                events.append((stat_due, f"Statutory {mo_str} (projected)", preset_stat))
        cur = nxt_month

    # 4) Recurring monthly operating spend on the 1st — override or last-month actual
    last_mo_end = date(today.year, today.month, 1) - timedelta(days=1)
    last_mo_str = f"{last_mo_end:%b %Y}"
    auto_opex = db.query(func.coalesce(func.sum(M.Payment.amount), 0)) \
        .filter(M.Payment.month == last_mo_str, M.Payment.status != "Void",
                M.Payment.category != "Salary").scalar() or 0
    monthly_opex = _setting_float("CF_MONTHLY_OPEX")
    if monthly_opex is None:
        monthly_opex = round(auto_opex, 2)
    cur = date(today.year, today.month, 1)
    while cur <= horizon_end:
        if cur > today and monthly_opex > 0:
            events.append((cur, "Recurring monthly opex (projected)", monthly_opex))
        cur = date(cur.year + (1 if cur.month == 12 else 0), (cur.month % 12) + 1, 1)

    # Bucket into weeks
    weeks = []
    running = opening_cash
    for w in range(n_weeks):
        w_start = today + timedelta(days=7 * w)
        w_end = w_start + timedelta(days=6)
        w_events = [(d, lbl, amt) for d, lbl, amt in events if w_start <= d <= w_end]
        outflow = sum(a for _, _, a in w_events)
        inflow = weekly_sales
        net = inflow - outflow
        running += net
        weeks.append({"start": w_start, "end": w_end, "inflow": round(inflow, 2),
                      "outflow": round(outflow, 2), "net": round(net, 2),
                      "closing": round(running, 2),
                      "due": sorted(w_events)})   # not "items" — dict.items() shadows it in Jinja
    lowest = min(weeks, key=lambda x: x["closing"]) if weeks else None

    return render(request, db, "cashflow.html", "cashflow",
                  opening_cash=round(opening_cash, 2), cash_split=cash_split,
                  weekly_sales=round(weekly_sales, 2), auto_weekly_sales=auto_weekly_sales,
                  monthly_opex=round(monthly_opex, 2), auto_opex=round(auto_opex, 2),
                  n_weeks=n_weeks, weeks=weeks, lowest=lowest, settings=settings,
                  last_mo_str=last_mo_str)


# ─────────────────────────── GENERAL LEDGER ───────────────────────────
@app.get("/reports/audit-log", response_class=HTMLResponse)
def audit_log(request: Request, user_id: int = 0, action: str = "", db: Session = Depends(get_db)):
    q = db.query(M.AuditLog)
    if user_id:
        q = q.filter(M.AuditLog.user_id == user_id)
    if action:
        q = q.filter(M.AuditLog.action.contains(action))
    entries = q.order_by(M.AuditLog.id.desc()).limit(500).all()
    users = db.query(M.User).order_by(M.User.display_name).all()
    blocked_count = db.query(M.AuditLog).filter(M.AuditLog.blocked == True).count()  # noqa: E712
    return render(request, db, "audit_log.html", "auditlog",
                  entries=entries, users=users, f_user_id=user_id, f_action=action,
                  blocked_count=blocked_count)


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
                                "cin": 0, "cout": p.amount, "link": f"/payments/{p.id}"})
    if kind in ("", "Sale"):
        for s in db.query(M.SalesEntry).all():
            if in_range(s.date) and match(s.stream, s.description, s.method):
                entries.append({"date": s.date, "code": s.stream, "type": "Sale",
                                "party": s.stream, "desc": s.description,
                                "cin": s.amount, "cout": 0, "link": f"/sales?month={s.month}",
                                "match_key": ("Sale", s.id)})
    if kind in ("", "Petty Cash"):
        for e in db.query(M.PettyCashEntry).all():
            if in_range(e.date) and match(e.description, e.category, e.recorded_by):
                entries.append({"date": e.date, "code": "PC", "type": "Petty Cash",
                                "party": e.recorded_by, "desc": e.description,
                                "cin": e.amount_in, "cout": e.amount_out, "link": f"/pettycash?month={e.month}",
                                "match_key": ("PettyCash", e.id)})
    if kind in ("", "Voucher"):
        for v in db.query(M.Voucher).all():
            if in_range(v.date) and match(v.pv_no, v.payee, v.status):
                entries.append({"date": v.date, "code": v.pv_no, "type": "Voucher",
                                "party": v.payee, "desc": f"Voucher · {v.status}",
                                "cin": 0, "cout": v.total, "link": f"/vouchers/{v.id}",
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

    # Journal entries that aren't derived from an operational record — opening
    # balances and manual adjustments. Without these the GL silently omits real
    # postings: right after go-live the books held only the opening entry, so
    # the GL looked completely empty while the Balance Sheet showed RM895k.
    if kind in ("", "Journal"):
        for je in db.query(M.JournalEntry).filter(
                M.JournalEntry.source_type.in_(("Opening", "Manual"))).all():
            if not in_range(je.date):
                continue
            for line in je.lines:
                acct = line.account
                if not match(je.ref, je.memo, line.description, acct.code, acct.name):
                    continue
                entries.append({
                    "date": je.date, "code": je.ref or f"JE-{je.id}", "type": "Journal",
                    "party": f"{acct.code} {acct.name}",
                    "desc": line.description or je.memo,
                    "cin": line.debit, "cout": line.credit, "is_journal": True,
                    "link": f"/accounting/ledger/{acct.id}"})

    matched_keys = {(l.matched_type, l.matched_id) for l in
                    db.query(M.BankStatementLine).filter(M.BankStatementLine.matched == True).all()}  # noqa: E712
    for e in entries:
        key = e.pop("match_key", None)
        e["reconciled"] = (key in matched_keys) if key else None
        e.setdefault("is_journal", False)

    entries.sort(key=lambda e: (e["date"], e["code"]), reverse=True)
    # Journal rows are debits/credits, not cash movements — including them in
    # the "money in / money out" cards would show RM1m of cash that never moved.
    total_in = sum(e["cin"] for e in entries if not e["is_journal"])
    total_out = sum(e["cout"] for e in entries if not e["is_journal"])
    journal_count = sum(1 for e in entries if e["is_journal"])
    kinds = ["Payment", "Sale", "Petty Cash", "Voucher", "Listing", "Payroll", "Journal"]
    return render(request, db, "general_ledger.html", "gl", entries=entries[:500],
                  q=q, frm=frm, to=to, kind=kind, kinds=kinds,
                  total_in=total_in, total_out=total_out, count=len(entries),
                  journal_count=journal_count)


# ─────────────────────────── RECEIVABLES + AR AGING ───────────────────────────
AR_BUCKETS = ["Current", "1–30 days", "31–60 days", "61–90 days", "90+ days"]


def _ar_bucket(inv, today):
    days = (today - inv.due_date).days
    if days <= 0:
        return 0
    if days <= 30:
        return 1
    if days <= 60:
        return 2
    if days <= 90:
        return 3
    return 4


def _ar_aging_rows(db):
    """Outstanding invoices bucketed by days overdue, grouped per customer."""
    today = date.today()
    open_invs = [i for i in db.query(M.ARInvoice).filter(M.ARInvoice.status != "Void").all()
                 if i.outstanding > 0.005]
    per_customer = {}
    for inv in open_invs:
        row = per_customer.setdefault(inv.customer, [0.0] * 5)
        row[_ar_bucket(inv, today)] += inv.outstanding
    rows = [{"customer": c, "buckets": [round(b, 2) for b in v], "total": round(sum(v), 2)}
            for c, v in sorted(per_customer.items())]
    totals = [round(sum(r["buckets"][i] for r in rows), 2) for i in range(5)]
    return rows, totals, round(sum(t for t in totals), 2), open_invs


def _build_invoice_pdf(db, inv) -> str:
    """Render (or re-render) the customer-facing PDF for an AR invoice and
    store its path on the record. Called on creation and again after a
    receipt, so the document always reflects what has actually been paid:
    with nothing received it shows a payment schedule, and once money is in
    it shows the deposit deducted and the balance still due."""
    settings = {s.key: s.value for s in db.query(M.Setting).all()}
    bank = {"bank_name": settings.get("COMPANY_BANK", ""),
            "account_no": settings.get("COMPANY_BANK_ACCOUNT", ""),
            "account_holder": settings.get("COMPANY_NAME", "")}
    if not (bank["bank_name"] or bank["account_no"]):
        acc = db.query(M.BankAccount).filter(M.BankAccount.active == True).first()  # noqa: E712
        if acc:
            bank = {"bank_name": acc.bank_name, "account_no": acc.account_no,
                    "account_holder": settings.get("COMPANY_NAME", "")}
    received = inv.received
    schedule = None
    if not received:
        schedule = [{"label": f"{inv.stream} — full amount", "amount": inv.amount,
                     "due": f"On or before {inv.due_date:%d/%m/%Y}", "status": "Due"}]
    # The description doubles as the line item, so don't repeat it in Notes —
    # that block is for payment terms the customer needs spelled out.
    terms = (f"Deposit of RM{received:,.2f} received. Balance of "
             f"RM{inv.outstanding:,.2f} due on or before "
             f"{inv.due_date:%d/%m/%Y}.") if received and inv.outstanding > 0.005 else ""
    return pdfgen.invoice_pdf(
        inv_no=inv.inv_no, customer=inv.customer,
        cust_address=inv.cust_address or "", cust_contact=inv.cust_contact or "",
        items=[{"description": inv.notes or f"{inv.stream} services",
                "amount": inv.amount}],
        due_date=f"{inv.due_date:%d/%m/%Y}",
        notes=terms,
        deposit_paid=received, schedule=schedule,
        company=settings.get("COMPANY_NAME", "MEOW & ME PET SHOP SDN BHD"),
        address=settings.get("COMPANY_ADDRESS", ""),
        reg_no=settings.get("COMPANY_ROC", ""), bank=bank)


@app.get("/receivables", response_class=HTMLResponse)
def receivables(request: Request, q: str = "", status: str = "",
                db: Session = Depends(get_db)):
    ledger.sync_ledger(db)
    query = db.query(M.ARInvoice).order_by(M.ARInvoice.date.desc(), M.ARInvoice.id.desc())
    if status:
        query = query.filter(M.ARInvoice.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((M.ARInvoice.inv_no.ilike(like)) | (M.ARInvoice.customer.ilike(like)))
    invoices = query.limit(300).all()
    open_total = sum(i.outstanding for i in db.query(M.ARInvoice)
                     .filter(M.ARInvoice.status != "Void").all() if i.outstanding > 0.005)
    return render(request, db, "receivables.html", "receivables",
                  invoices=invoices, q=q, flt=status, streams=M.STREAMS,
                  open_total=round(open_total, 2), today_iso=date.today().isoformat(),
                  flash=request.session.pop("flash_ar", None))


@app.post("/receivables/new")
async def receivables_new(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    f = await request.form()
    customer = str(f.get("customer", "")).strip()
    amount = float(f.get("amount") or 0)
    if not customer or amount <= 0:
        return RedirectResponse("/receivables", status_code=303)
    inv_date = parse_date(str(f.get("date", ""))) or date.today()
    due = parse_date(str(f.get("due_date", ""))) if f.get("due_date") else None
    inv_no = telegram_bot.next_monthly_counter(db, "ARINV", "INV-", inv_date)
    inv = M.ARInvoice(inv_no=inv_no, customer=customer,
                      cust_address=str(f.get("cust_address", "")).strip(),
                      cust_contact=str(f.get("cust_contact", "")).strip(),
                      stream=str(f.get("stream") or "Boarding"),
                      date=inv_date, due_date=due or (inv_date + timedelta(days=30)),
                      amount=amount, month=f"{inv_date:%b %Y}",
                      notes=str(f.get("notes", "")).strip(),
                      created_by=user.display_name)
    db.add(inv)
    db.flush()
    try:
        inv.pdf_path = _build_invoice_pdf(db, inv)
    except Exception:
        inv.pdf_path = ""   # never block the accounting record on a PDF failure
    db.commit()
    ledger.sync_ledger(db)
    request.session["flash_ar"] = (
        f"{inv_no} · {customer} · RM {amount:,.2f} — invoice PDF ready, posted "
        f"Dr Trade Debtors / Cr {f.get('stream') or 'Boarding'} revenue")
    return RedirectResponse("/receivables", status_code=303)


@app.post("/receivables/{inv_id}/pdf")
def receivables_pdf(inv_id: int, request: Request, db: Session = Depends(get_db)):
    """Re-issue the PDF — picks up edited company settings and any receipts
    recorded since the invoice was created."""
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    inv = db.get(M.ARInvoice, inv_id)
    if inv:
        try:
            inv.pdf_path = _build_invoice_pdf(db, inv)
            db.commit()
            request.session["flash_ar"] = f"{inv.inv_no} PDF re-issued"
        except Exception:
            request.session["flash_ar"] = f"Could not generate the PDF for {inv.inv_no}"
    return RedirectResponse("/receivables", status_code=303)


@app.post("/receivables/{inv_id}/receipt")
async def receivables_receipt(inv_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    inv = db.get(M.ARInvoice, inv_id)
    f = await request.form()
    amount = float(f.get("amount") or 0)
    if inv and inv.status != "Void" and amount > 0:
        db.add(M.ARReceipt(invoice_id=inv.id,
                           date=parse_date(str(f.get("date", ""))) or date.today(),
                           amount=amount, method=str(f.get("method") or "Bank"),
                           recorded_by=user.display_name))
        db.flush()
        if inv.outstanding <= 0.005:
            inv.status = "Paid"
        # Re-issue so the document shows the deposit received and the balance
        # still due, rather than the original "nothing paid yet" schedule.
        try:
            inv.pdf_path = _build_invoice_pdf(db, inv)
        except Exception:
            pass
        db.commit()
        ledger.sync_ledger(db)
        request.session["flash_ar"] = f"Receipt RM {amount:,.2f} against {inv.inv_no} recorded"
    return RedirectResponse("/receivables", status_code=303)


@app.post("/receivables/{inv_id}/void")
def receivables_void(inv_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    inv = db.get(M.ARInvoice, inv_id)
    if inv and not inv.receipts:
        inv.status = "Void"
        db.commit()
        ledger.sync_ledger(db)   # removes the derived posting
    return RedirectResponse("/receivables", status_code=303)


@app.get("/reports/ar-aging", response_class=HTMLResponse)
def ar_aging(request: Request, db: Session = Depends(get_db)):
    ledger.sync_ledger(db)
    rows, totals, grand, open_invs = _ar_aging_rows(db)
    today = date.today()
    inv_rows = sorted(({"inv": i, "days": max(0, (today - i.due_date).days),
                        "bucket": AR_BUCKETS[_ar_bucket(i, today)]} for i in open_invs),
                      key=lambda r: -r["days"])
    return render(request, db, "ar_aging.html", "araging",
                  rows=rows, totals=totals, grand=grand, buckets=AR_BUCKETS,
                  inv_rows=inv_rows)


# ─────────────────────────── STOCK & SERVICE USAGE ───────────────────────────
# Eugene's model: every service consumes a slice of stock ("1 grooming uses 5%
# of a shampoo bottle"). Recipes define the slice; the sessions count on each
# sales entry drives consumption. On-hand is always DERIVED (purchases and
# adjustments minus computed usage) — never stored — so it can't drift and is
# rebuildable, same philosophy as the ledger.
STOCK_CATEGORIES = ["Grooming Supplies", "Cat Supplies", "Cleaning", "Tools & Equipment", "Other"]


def _stock_state(db):
    items = db.query(M.StockItem).order_by(M.StockItem.category, M.StockItem.name).all()
    recipes = db.query(M.ServiceRecipe).filter(M.ServiceRecipe.active == True).all()  # noqa: E712
    by_id = {i.id: i for i in items}

    # Sessions per stream — total and per calendar month
    sess_stream, sess_month, rev_stream = {}, {}, {}
    for s in db.query(M.SalesEntry).filter(M.SalesEntry.qty > 0).all():
        sess_stream[s.stream] = sess_stream.get(s.stream, 0) + s.qty
        rev_stream[s.stream] = rev_stream.get(s.stream, 0) + s.amount
        mk = s.date.replace(day=1)
        sess_month.setdefault(mk, {})
        sess_month[mk][s.stream] = sess_month[mk].get(s.stream, 0) + s.qty

    # Usage per item (units, all time) = Σ sessions × recipe
    usage = {}
    for r in recipes:
        if r.item_id in by_id:
            usage[r.item_id] = usage.get(r.item_id, 0) + sess_stream.get(r.stream, 0) * r.qty_per_service

    rows = []
    for i in items:
        bought = sum(m.qty for m in i.movements)
        used = usage.get(i.id, 0.0)
        on_hand = round(bought - used, 3)
        rows.append({"item": i, "bought": round(bought, 2), "used": round(used, 2),
                     "on_hand": on_hand, "value": round(on_hand * i.unit_cost, 2),
                     "low": i.reorder_level > 0 and on_hand <= i.reorder_level})

    # Cost per service and margin per stream (only streams with recipes)
    streams = {}
    for r in recipes:
        i = by_id.get(r.item_id)
        if not i:
            continue
        st = streams.setdefault(r.stream, {"lines": [], "cost": 0.0})
        line_cost = round(r.qty_per_service * i.unit_cost, 4)
        row = next((x for x in rows if x["item"].id == i.id), None)
        st["lines"].append({"recipe": r, "item": i, "cost": line_cost,
                            "services_left": round(row["on_hand"] / r.qty_per_service, 1)
                            if row and r.qty_per_service > 0 else None})
        st["cost"] = round(st["cost"] + line_cost, 4)
    for stname, st in streams.items():
        n = sess_stream.get(stname, 0)
        st["sessions"] = n
        st["rev_per_service"] = round(rev_stream.get(stname, 0) / n, 2) if n else None
        st["margin"] = round(st["rev_per_service"] - st["cost"], 2) if n else None

    # Last-6-months usage cost for the trend chart
    today = date.today()
    months = []
    mk = date(today.year, today.month, 1)
    for _ in range(6):
        months.append(mk)
        mk = (mk - timedelta(days=1)).replace(day=1)
    months.reverse()
    trend = []
    for mk in months:
        cost = sessions = 0.0
        for r in recipes:
            i = by_id.get(r.item_id)
            if i:
                cost += sess_month.get(mk, {}).get(r.stream, 0) * r.qty_per_service * i.unit_cost
        for v in sess_month.get(mk, {}).values():
            sessions += v
        trend.append({"label": f"{mk:%b}", "month": f"{mk:%b %Y}",
                      "cost": round(cost, 2), "sessions": int(sessions)})
    return rows, streams, trend


@app.get("/stock", response_class=HTMLResponse)
def stock_page(request: Request, db: Session = Depends(get_db)):
    rows, streams, trend = _stock_state(db)
    movements = db.query(M.StockMovement).order_by(
        M.StockMovement.date.desc(), M.StockMovement.id.desc()).limit(50).all()
    max_cost = max([t["cost"] for t in trend] + [1])
    return render(request, db, "stock.html", "stock",
                  rows=rows, streams=streams, trend=trend, max_cost=max_cost,
                  movements=movements, categories=STOCK_CATEGORIES,
                  sales_streams=M.STREAMS, today_iso=date.today().isoformat(),
                  total_value=round(sum(r["value"] for r in rows), 2),
                  low_count=sum(1 for r in rows if r["low"]),
                  flash=request.session.pop("flash_stock", None))


@app.post("/stock/item/new")
async def stock_item_new(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    f = await request.form()
    name = str(f.get("name", "")).strip()
    if not name:
        return RedirectResponse("/stock", status_code=303)
    item = M.StockItem(name=name, category=str(f.get("category") or "Grooming Supplies"),
                       unit=str(f.get("unit", "pcs")).strip() or "pcs",
                       unit_cost=float(f.get("unit_cost") or 0),
                       reorder_level=float(f.get("reorder_level") or 0),
                       notes=str(f.get("notes", "")).strip())
    db.add(item)
    db.flush()
    opening = float(f.get("opening_qty") or 0)
    if opening:
        db.add(M.StockMovement(item_id=item.id, date=date.today(), qty=opening,
                               kind="Adjustment", notes="Opening stock count",
                               unit_cost=item.unit_cost, recorded_by=user.display_name))
    db.commit()
    request.session["flash_stock"] = f"Item added: {name}"
    return RedirectResponse("/stock", status_code=303)


@app.post("/stock/item/{item_id}/update")
async def stock_item_update(item_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    item = db.get(M.StockItem, item_id)
    if item:
        f = await request.form()
        if f.get("unit_cost") is not None and f.get("unit_cost") != "":
            item.unit_cost = float(f.get("unit_cost") or 0)
        if f.get("reorder_level") is not None and f.get("reorder_level") != "":
            item.reorder_level = float(f.get("reorder_level") or 0)
        if f.get("toggle"):
            item.active = not item.active
        db.commit()
    return RedirectResponse("/stock", status_code=303)


@app.post("/stock/move")
async def stock_move(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    f = await request.form()
    item = db.get(M.StockItem, int(f.get("item_id") or 0))
    qty = float(f.get("qty") or 0)
    if item and qty:
        kind = str(f.get("kind") or "Purchase")
        if kind not in ("Purchase", "Adjustment"):
            kind = "Purchase"
        if kind == "Adjustment" and str(f.get("direction") or "") == "out":
            qty = -abs(qty)
        unit_cost = float(f.get("unit_cost") or 0)
        db.add(M.StockMovement(item_id=item.id,
                               date=parse_date(str(f.get("date", ""))) or date.today(),
                               qty=qty, kind=kind, ref=str(f.get("ref", "")).strip(),
                               unit_cost=unit_cost, notes=str(f.get("notes", "")).strip(),
                               recorded_by=user.display_name))
        # Latest purchase price becomes the costing price for the recipes.
        if kind == "Purchase" and unit_cost > 0:
            item.unit_cost = unit_cost
        db.commit()
        request.session["flash_stock"] = f"{kind} recorded: {item.name} {qty:+g} {item.unit}"
    return RedirectResponse("/stock", status_code=303)


@app.post("/stock/recipe/new")
async def stock_recipe_new(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    f = await request.form()
    item = db.get(M.StockItem, int(f.get("item_id") or 0))
    amount = float(f.get("amount") or 0)
    if item and amount > 0:
        # Entered either as "% of one unit" (Eugene's mental model) or in units.
        qty = amount / 100.0 if str(f.get("mode") or "pct") == "pct" else amount
        db.add(M.ServiceRecipe(stream=str(f.get("stream") or "Grooming"),
                               item_id=item.id, qty_per_service=qty))
        db.commit()
        request.session["flash_stock"] = (
            f"Recipe added: 1 {f.get('stream') or 'Grooming'} uses "
            f"{qty:g} {item.unit} of {item.name}")
    return RedirectResponse("/stock", status_code=303)


@app.post("/stock/recipe/{rid}/delete")
def stock_recipe_delete(rid: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role not in ("admin", "manager"):
        return RedirectResponse("/", status_code=302)
    r = db.get(M.ServiceRecipe, rid)
    if r:
        db.delete(r)
        db.commit()
    return RedirectResponse("/stock", status_code=303)


# ─────────────────────────── SALES / PURCHASE LEDGERS ───────────────────────────
@app.get("/reports/sales-ledger", response_class=HTMLResponse)
def sales_ledger(request: Request, frm: str = "", to: str = "",
                 db: Session = Depends(get_db)):
    """Sales day book: every cash sale and credit invoice in the period, with
    per-stream totals — Weng Teng's 'sales ledger'."""
    d_from, d_to = parse_date(frm) if frm else None, parse_date(to) if to else None

    def in_range(d):
        return (not d_from or d >= d_from) and (not d_to or d <= d_to)

    rows = []
    for s in db.query(M.SalesEntry).all():
        if in_range(s.date):
            rows.append({"date": s.date, "ref": "Sale", "kind": "Cash sale",
                         "party": s.recorded_by or "-", "stream": s.stream,
                         "desc": s.description, "amount": s.amount})
    for i in db.query(M.ARInvoice).filter(M.ARInvoice.status != "Void").all():
        if in_range(i.date):
            rows.append({"date": i.date, "ref": i.inv_no, "kind": "Credit invoice",
                         "party": i.customer, "stream": i.stream,
                         "desc": i.notes or "Customer invoice", "amount": i.amount})
    rows.sort(key=lambda r: (r["date"], r["ref"]))
    stream_totals = {}
    for r in rows:
        stream_totals[r["stream"]] = round(stream_totals.get(r["stream"], 0) + r["amount"], 2)
    return render(request, db, "sales_ledger.html", "salesledger",
                  rows=rows, frm=frm, to=to, stream_totals=stream_totals,
                  grand=round(sum(r["amount"] for r in rows), 2))


@app.get("/reports/purchase-ledger", response_class=HTMLResponse)
def purchase_ledger(request: Request, frm: str = "", to: str = "", supplier: str = "",
                    db: Session = Depends(get_db)):
    """Purchase ledger: supplier-by-supplier account of everything bought,
    what's been paid, and what's still owing."""
    d_from, d_to = parse_date(frm) if frm else None, parse_date(to) if to else None
    q = db.query(M.Payment).filter(M.Payment.status != "Void")
    if d_from:
        q = q.filter(M.Payment.date >= d_from)
    if d_to:
        q = q.filter(M.Payment.date <= d_to)
    if supplier:
        q = q.filter(M.Payment.supplier == supplier)
    pays = q.order_by(M.Payment.supplier, M.Payment.date, M.Payment.id).all()
    groups = {}
    for p in pays:
        g = groups.setdefault(p.supplier or "(no supplier)", {"rows": [], "total": 0.0, "unpaid": 0.0})
        g["rows"].append(p)
        g["total"] = round(g["total"] + p.amount, 2)
        if p.status != "Paid":
            g["unpaid"] = round(g["unpaid"] + p.amount, 2)
    suppliers = [s for (s,) in db.query(M.Payment.supplier).distinct()
                 .order_by(M.Payment.supplier).all() if s]
    return render(request, db, "purchase_ledger.html", "purchledger",
                  groups=groups, frm=frm, to=to, supplier=supplier, suppliers=suppliers,
                  grand=round(sum(g["total"] for g in groups.values()), 2),
                  grand_unpaid=round(sum(g["unpaid"] for g in groups.values()), 2))


# ─────────────────────────── BANK RECONCILIATION REPORT ───────────────────────────
@app.get("/reconciliation/report", response_class=HTMLResponse)
def reconciliation_report(request: Request, account: int = 0, frm: str = "", to: str = "",
                          db: Session = Depends(get_db)):
    """Printable reconciliation statement for one bank account and period:
    balance proof, matched lines with what they matched to, unmatched lines."""
    accounts = db.query(M.BankAccount).filter(M.BankAccount.active == True).order_by(M.BankAccount.id).all()  # noqa: E712
    acc = db.get(M.BankAccount, account) if account else (accounts[0] if accounts else None)
    d_from, d_to = parse_date(frm) if frm else None, parse_date(to) if to else None
    lines = []
    if acc:
        q = db.query(M.BankStatementLine).filter(M.BankStatementLine.bank_account_id == acc.id)
        if d_from:
            q = q.filter(M.BankStatementLine.date >= d_from)
        if d_to:
            q = q.filter(M.BankStatementLine.date <= d_to)
        lines = q.order_by(M.BankStatementLine.date, M.BankStatementLine.id).all()
    matched = [l for l in lines if l.matched]
    unmatched = [l for l in lines if not l.matched]
    reconciled_total = round(sum(l.amount for l in matched), 2)
    unreconciled_total = round(sum(l.amount for l in unmatched), 2)
    opening_balance = acc.opening_balance if acc else 0.0
    book_candidates = _unmatched_system_txns(db, acc.id) if acc else []
    return render(request, db, "reconciliation_report.html", "reconciliation",
                  accounts=accounts, acc=acc, frm=frm, to=to,
                  matched=matched, unmatched=unmatched,
                  reconciled_total=reconciled_total, unreconciled_total=unreconciled_total,
                  opening_balance=opening_balance,
                  balance_per_books=round(opening_balance + reconciled_total, 2),
                  balance_per_bank=round(opening_balance + reconciled_total + unreconciled_total, 2),
                  book_candidates=book_candidates)


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
    sup_ids = {s.name.lower(): s.id for s in db.query(M.Supplier).all()}
    return render(request, db, "ap_aging.html", "apaging",
                  rows=rows, buckets=buckets, totals=totals, grand=grand, today=today,
                  supplier_names=supplier_names, f_supplier=supplier, f_bucket=bucket,
                  f_status=status, sup_ids=sup_ids)


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
def pnl(request: Request, month: str = "", frm: str = "", to: str = "",
        db: Session = Depends(get_db)):
    """Monthly P&L, or a date range when frm/to are given.

    The month list runs continuously from the accounts' start date to today —
    previously it only listed months that already had transactions, so a month
    with no activity simply couldn't be opened, and there was no way to look at
    a quarter or a full year at once."""
    start = _accounts_start_month(db)
    all_months = _months_between(start, month_str())

    # Date-range mode: frm/to are "Mon YYYY" strings picked from the same list.
    range_mode = bool(frm and to)
    if range_mode:
        try:
            i, j = all_months.index(frm), all_months.index(to)
        except ValueError:
            range_mode = False
        else:
            if i > j:
                i, j = j, i
            sel_months = all_months[i:j + 1]
            mo = f"{frm} – {to}"
    if not range_mode:
        mo = month or month_str()
        sel_months = [mo]
    months = all_months

    # Revenue
    revenue = dict(db.query(M.SalesEntry.stream, func.sum(M.SalesEntry.amount))
                   .filter(M.SalesEntry.month.in_(sel_months))
                   .group_by(M.SalesEntry.stream).all())
    total_rev = sum(revenue.values())

    # Payments in month, by group
    pays = db.query(M.Payment).filter(M.Payment.month.in_(sel_months),
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
    for e in db.query(M.PettyCashEntry).filter(M.PettyCashEntry.month.in_(sel_months),
                                               M.PettyCashEntry.amount_out > 0).all():
        cat = e.category or "Uncategorized"
        grp = M.group_for(cat)
        row = SimpleNamespace(pay_no="PC", supplier=e.recorded_by, id=None,
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
        .filter(M.PayrollRun.month.in_(sel_months),
                M.PayrollRun.status == "Confirmed").scalar()

    gross_profit = total_rev - total_cogs
    total_operating = total_opex + total_other + payroll_total
    net = gross_profit - total_operating

    return render(request, db, "pnl.html", "pnl", month=mo, months=months,
                  range_mode=range_mode, f_frm=frm, f_to=to,
                  sel_count=len(sel_months),
                  revenue=revenue, total_rev=total_rev,
                  cogs=cogs, total_cogs=total_cogs, gross_profit=gross_profit,
                  opex=opex, total_opex=total_opex, other=other, total_other=total_other,
                  payroll_total=payroll_total,
                  total_operating=total_operating, net=net,
                  capex=capex, total_capex=total_capex)


# ─────────────────────────── SETTINGS ───────────────────────────
# ─────────────────────────── BACKUPS ───────────────────────────
@app.get("/settings/backups", response_class=HTMLResponse)
def backups_page(request: Request, db: Session = Depends(get_db)):
    snaps = backup.list_snapshots()
    total = sum(s["size"] for s in snaps)
    return render(request, db, "backups.html", "backups",
                  snapshots=snaps, total_size=backup._fmt_size(total),
                  keep=backup.KEEP_SNAPSHOTS, backup_dir=backup.BACKUP_DIR,
                  db_file=backup.sqlite_file())


@app.post("/settings/backups/create")
def backups_create(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    backup.make_snapshot("manual")
    return RedirectResponse("/settings/backups", status_code=302)


@app.get("/settings/backups/download/{name}")
def backups_download(name: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    path = backup.snapshot_path(name)
    if not path:
        raise HTTPException(404)
    audit.log_action(db, user, f"Downloaded database snapshot ({name})",
                     f"/settings/backups/download/{name}")
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@app.get("/settings/backups/download-full")
def backups_download_full(request: Request, db: Session = Depends(get_db)):
    """Database + every uploaded document, as one zip. This is the copy that
    should live somewhere other than Render."""
    user = current_user(request, db)
    if not user or user.role != "admin":
        return RedirectResponse("/", status_code=302)
    import tempfile
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    fname = f"catday_full_backup_{stamp}.zip"
    tmp = os.path.join(tempfile.gettempdir(), fname)
    info = backup.build_full_archive(tmp)
    audit.log_action(db, user,
                     f"Downloaded FULL backup ({info['files']} files, "
                     f"{backup._fmt_size(info['size'])})",
                     "/settings/backups/download-full")
    return FileResponse(tmp, filename=fname, media_type="application/zip")


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
                "SST_REGISTERED", "SST_NUMBER", "COMPANY_ROC", "COMPANY_BANK",
                "COMPANY_BANK_ACCOUNT", "COMPANY_TIN", "COMPANY_MSIC",
                "PREFIX_DOC", "PREFIX_PAY", "PREFIX_PV", "PREFIX_PL",
                "CF_WEEKS", "CF_WEEKLY_SALES", "CF_MONTHLY_OPEX"):
        if key in form:
            s = db.get(M.Setting, key)
            if not s:
                s = M.Setting(key=key)
                db.add(s)
            s.value = str(form[key])
    db.commit()
    nxt = form.get("next", "/settings")
    return RedirectResponse(nxt if str(nxt).startswith("/") else "/settings", status_code=302)


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


@app.get("/search", response_class=HTMLResponse)
def global_search(request: Request, q: str = "", db: Session = Depends(get_db)):
    """One box for 'where is that thing?' — codes, suppliers, invoice numbers,
    descriptions. An exact code match jumps straight to the record."""
    ql = q.strip().lower()
    if not ql:
        return render(request, db, "search.html", "dashboard", q=q, groups=[], jumped=False)

    like = f"%{ql}%"
    payments = db.query(M.Payment).filter(
        func.lower(M.Payment.pay_no).like(like) | func.lower(M.Payment.supplier).like(like)
        | func.lower(M.Payment.description).like(like) | func.lower(M.Payment.invoice_no).like(like)
    ).order_by(M.Payment.date.desc()).limit(25).all()
    vouchers = db.query(M.Voucher).filter(
        func.lower(M.Voucher.pv_no).like(like) | func.lower(M.Voucher.payee).like(like)
    ).order_by(M.Voucher.date.desc()).limit(25).all()
    suppliers = db.query(M.Supplier).filter(
        func.lower(M.Supplier.name).like(like) | func.lower(M.Supplier.account_no).like(like)
    ).limit(25).all()
    docs = db.query(M.Document).filter(
        func.lower(M.Document.doc_no).like(like) | func.lower(M.Document.supplier).like(like)
        | func.lower(M.Document.description).like(like)
    ).order_by(M.Document.id.desc()).limit(25).all()
    listings = db.query(M.Listing).filter(func.lower(M.Listing.pl_no).like(like)).limit(25).all()

    # Exact code match → go straight there instead of showing a one-row list
    for p in payments:
        if p.pay_no.lower() == ql:
            return RedirectResponse(f"/payments/{p.id}", status_code=302)
    for v in vouchers:
        if v.pv_no.lower() == ql:
            return RedirectResponse(f"/vouchers/{v.id}", status_code=302)
    for s in suppliers:
        if s.name.lower() == ql:
            return RedirectResponse(f"/suppliers/{s.id}", status_code=302)

    groups = [
        ("Payments", [(p.pay_no, f"{p.supplier or '—'} · {p.description[:50]}",
                       p.amount, f"/payments/{p.id}", p.status) for p in payments]),
        ("Vouchers", [(v.pv_no, v.payee, v.total, f"/vouchers/{v.id}", v.status) for v in vouchers]),
        ("Suppliers", [(s.name, f"{s.sup_type} · {s.bank_name or 'no bank on file'}",
                        None, f"/suppliers/{s.id}", "Active" if s.active else "Inactive")
                       for s in suppliers]),
        ("Documents", [(d.doc_no, f"{d.supplier or d.sender} · {d.description[:50]}",
                        d.amount, "/documents", d.status) for d in docs]),
        ("Listings", [(l.pl_no, f"{len(l.vouchers)} vouchers", l.total, "/listings", l.status)
                      for l in listings]),
    ]
    groups = [(name, rows) for name, rows in groups if rows]
    return render(request, db, "search.html", "dashboard", q=q, groups=groups, jumped=False)


@app.get("/health")
def health():
    # RENDER_GIT_COMMIT is set by Render at deploy time — lets us verify
    # from outside exactly which commit is serving.
    # `brand` reports whether the cat day fonts and logo actually loaded in
    # THIS environment: pdfgen falls back to Helvetica rather than failing, so
    # without this a deploy missing its assets would silently produce
    # off-brand PDFs and nobody would notice until a customer saw one.
    return {"status": "ok", "app": "CATDAY System",
            "build": os.environ.get("RENDER_GIT_COMMIT", "local")[:10],
            "brand": {"fonts": pdfgen.DISPLAY_F.startswith("Gliker"),
                      "logo": os.path.exists(pdfgen.LOGO_CREAM)}}
