"""Bills straight from Telegram: Verify, then Raise PV & listing.

Someone drops a bill (TNB, Air Selangor, Unifi, Coway, a supplier invoice) in
the group. The bot reads it and replies with what it found plus a Verify
button. Verify records the payment exactly as the web verification would.
A second button then raises the voucher and puts it on this month's Telegram
listing, so a month's bills end up together on one listing for approval.

Utilities are paid by JomPAY, not by transfer to an account, so the biller
code and Ref-1 printed on the bill travel with the payment onto the voucher
and the listing (Payment.pay_ref / Voucher.pay_ref).

Raising a PV is preparation only. Nothing is paid until the listing is
approved and processed, which is why anyone in the group may press these.
"""
import json
from datetime import date, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from . import models as M

TAG = "(Telegram)"   # marks the listing the bot fills, so manual listings are never touched


# ───────────────────────── reading what the AI stored ─────────────────────────
def payload(doc) -> dict:
    try:
        return json.loads(doc.payload_json or "{}")
    except ValueError:
        return {}


def pay_ref_from(pl: dict) -> str:
    """The bill's own payment instruction, or "" for a normal bank transfer.
    Labels are single words: the voucher PDF splits on them."""
    jp = pl.get("jompay") or {}
    if str(jp.get("biller_code") or "").strip() and str(jp.get("ref1") or "").strip():
        out = f"JomPAY · Biller {jp['biller_code'].strip()} · Ref-1 {jp['ref1'].strip()}"
        if str(jp.get("ref2") or "").strip():
            out += f" · Ref-2 {jp['ref2'].strip()}"
        return out
    va = pl.get("pay_to_account") or {}
    if str(va.get("account_no") or "").strip():
        out = f"{(va.get('bank_name') or 'Bank').strip()} · Account {va['account_no'].strip()}"
        if str(va.get("reference") or "").strip():
            out += f" · Reference {va['reference'].strip()}"
        return out
    return ""


def due_date_from(pl: dict):
    try:
        return datetime.strptime(str(pl.get("due_date") or ""), "%Y-%m-%d").date()
    except ValueError:
        return None


def previous_balance(doc, pl: dict) -> float:
    """Unpaid amount from earlier bills inside this bill's total. Only trusted
    when it is a real part of the total."""
    try:
        prev = round(float(pl.get("previous_balance") or 0), 2)
    except (TypeError, ValueError):
        return 0.0
    return prev if 0 < prev < (doc.amount or 0) else 0.0


def _same_supplier(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a and b) and (a == b or a in b or b in a)


def find_earlier(db: Session, doc, prev: float, exclude_id=None) -> list:
    """Payments already in our books for the unpaid balance a bill carries.
    The earlier bill is often recorded with a small fee on top (Air Selangor
    RM 278.10 + RM 3.00 notice fee = PAY-0017 RM 281.10), so allow a few
    ringgit over. Not filtered by date: the earlier payment is often dated the
    day it was PAID (PAY-0017, 25 Sep), after this bill was issued (10 Sep).
    Payments raised from this same bill don't count. Closest first."""
    if not prev:
        return []
    inv = (doc.invoice_no or "").strip().lower()
    out = [p for p in db.query(M.Payment).filter(M.Payment.status != "Void").all()
           if p.id != exclude_id and _same_supplier(p.supplier, doc.supplier)
           and prev - 0.01 <= (p.amount or 0) <= prev + 15
           and not (inv and (p.invoice_no or "").strip().lower() == inv)]
    return sorted(out, key=lambda p: (abs((p.amount or 0) - prev), p.voucher_id is not None))


def amount_due(doc, pl: dict) -> float:
    """What the bill says is payable now; falls back to the total read."""
    v = pl.get("amount_due")
    try:
        return round(float(v), 2) if v not in (None, "") else round(doc.amount or 0, 2)
    except (TypeError, ValueError):
        return round(doc.amount or 0, 2)


def nothing_to_pay(doc, pl: dict) -> bool:
    """A credit already on the account covers this bill (Coway: charges 99.00,
    previous balance -99.00, outstanding 0.00). Paying it would overpay."""
    return amount_due(doc, pl) <= 0.005


def bill_month(doc, pl: dict) -> str:
    """Utilities are booked in the month of the bill date (the September P&L
    carried the TNB bill dated 19 Sep). Left to the AI, a bill covering
    15 Sep – 14 Oct comes back as Sep one time and Oct the next."""
    from .main import normalize_month
    if doc.doc_date and (pay_ref_from(pl).startswith("JomPAY") or doc.category == "Utilities"):
        return f"{doc.doc_date:%b %Y}"
    return normalize_month(doc.month, doc.doc_date or date.today())


def apply_bill_fields(p, pl: dict) -> None:
    """Copy payment instruction + due date onto a payment (web verify uses this too)."""
    p.pay_ref = pay_ref_from(pl)
    p.due_date = due_date_from(pl)


def is_payable(doc) -> bool:
    return (doc.status == "Pending" and doc.section in ("Purchase", "Expense")
            and doc.doc_type not in M.PROVISIONAL_DOC_TYPES and (doc.amount or 0) > 0)


# ───────────────────────── Telegram text & buttons ─────────────────────────
def _md(s) -> str:
    """Telegram Markdown (legacy) chokes on stray _ * ` [ — strip them from data."""
    return str(s or "").replace("_", " ").replace("*", "").replace("`", "'").replace("[", "(")


def summary_text(doc, db: Session | None = None) -> str:
    pl = payload(doc)
    prev = previous_balance(doc, pl)
    due = due_date_from(pl)
    ref = pay_ref_from(pl)
    lines = [f"🧾 *Bill received 账单已收到* · `{doc.doc_no}`", ""]
    lines.append(f"🏢 {_md(doc.supplier) or '— supplier not read —'}")
    if doc.invoice_no:
        lines.append(f"🔢 Invoice 单号: {_md(doc.invoice_no)}")
    lines.append(f"💰 *RM {doc.amount:,.2f}*")
    if nothing_to_pay(doc, pl):
        lines.append("    ✅ *Nothing to pay* — the bill shows RM 0.00 outstanding "
                     "(a credit already covers it) 无需付款")
    if prev:
        lines.append(f"    incl. RM {prev:,.2f} unpaid from an earlier bill 含前期欠款")
        lines.append(f"    this bill's own charges RM {doc.amount - prev:,.2f}")
        if db is not None:
            on_pv = [p for p in find_earlier(db, doc, prev) if p.voucher_id]
            if on_pv:
                v = db.get(M.Voucher, on_pv[0].voucher_id)
                lines.append(f"    ↳ the RM {prev:,.2f} is already on {v.pv_no if v else 'a voucher'} "
                             f"({on_pv[0].pay_no} RM {on_pv[0].amount:,.2f}) — "
                             f"this PV will pay *RM {doc.amount - prev:,.2f}* only")
    if doc.doc_date:
        lines.append(f"📅 Bill date 账单日期: {doc.doc_date:%d/%m/%Y} · books 入账: {bill_month(doc, pl)}")
    if due:
        late = (date.today() - due).days
        lines.append(f"⏰ Due 到期: {due:%d/%m/%Y}" + (f"  ⚠️ *OVERDUE {late} days 已逾期*" if late > 0 else ""))
    if not nothing_to_pay(doc, pl):
        lines.append(f"💳 Pay by 付款: {_md(ref)}" if ref
                     else "💳 Pay by 付款: bank transfer to the supplier's account on file")
    if doc.category:
        lines.append(f"📂 {_md(doc.category)}")
    if db is not None:
        from .main import find_duplicate_payments
        dups = find_duplicate_payments(db, doc.supplier, doc.invoice_no, doc.amount)
        for p, why, blocking in dups:
            lines.append(("⛔ " if blocking else "⚠️ ") + f"Already have {p.pay_no} ({_md(why)})")
    lines += ["", "Check the details, then tap *Verify*. Wrong? Fix it on the web.",
              "核对无误后按 *Verify*；有错请在网页修改。"]
    return "\n".join(lines)


def verify_buttons(doc):
    from .telegram_bot import BASE_URL
    return [[{"text": "✅ Verify 核实", "callback_data": f"bill:v:{doc.id}"}],
            [{"text": "✏️ Fix on web 网页修改", "url": f"{BASE_URL}/documents"}]]


def pv_buttons(doc):
    return [[{"text": "📋 Raise PV & listing 开付款单", "callback_data": f"bill:pv:{doc.id}"}]]


# ───────────────────────── the two actions ─────────────────────────
def verify_bill(db: Session, doc, who: str) -> tuple[bool, str]:
    """Record the payment(s) for this bill. Returns (ok, message)."""
    from .main import find_duplicate_payments, normalize_month, tax_of
    from . import ledger

    if doc.status != "Pending":
        return False, f"{doc.doc_no} is already {doc.status.lower()}"
    if not is_payable(doc):
        return False, "Not a bill to pay — open it on the web"
    if not doc.supplier:
        return False, "Supplier not read — fix it on the web first"
    blocking = [d for d in find_duplicate_payments(db, doc.supplier, doc.invoice_no, doc.amount) if d[2]]
    if blocking:
        return False, f"Already recorded as {blocking[0][0].pay_no} — not entered twice"

    from .telegram_bot import next_counter
    pl = payload(doc)
    if nothing_to_pay(doc, pl):
        # Nothing owed: keep the bill on file, raise no payment (the credit
        # that covers it was paid — and booked — earlier).
        doc.status, doc.section = "Verified", "Filing Only"
        doc.verified_by, doc.verified_at = who, datetime.utcnow()
        db.commit()
        return True, "Nothing to pay (RM 0.00 outstanding) — filed for record, no payment raised"
    prev = previous_balance(doc, pl)
    d = doc.doc_date or date.today()
    month = bill_month(doc, pl)
    category = doc.category or ""
    grp = M.group_for(category, doc.section)
    tax_type = pl.get("tax_type") or "None"
    current = round(doc.amount - prev, 2)

    def new_payment(amount, description, tax="None"):
        p = M.Payment(pay_no=next_counter(db, "PAY", "PAY-"), date=d, supplier=doc.supplier,
                      description=description, category=category, grp=grp, amount=amount,
                      month=month, invoice_no=doc.invoice_no, tax_type=tax,
                      tax_amount=tax_of(tax, amount),
                      status="Categorized" if category else "Unsorted",
                      notes=f"from {doc.doc_no} ({doc.sender}) · verified in Telegram by {who}")
        apply_bill_fields(p, pl)
        db.add(p)
        db.flush()
        return p

    main_p = new_payment(current, doc.description or f"{doc.supplier} {doc.invoice_no}", tax_type)
    ids, notes = [main_p.id], [f"{main_p.pay_no} RM {current:,.2f}"]

    if prev:
        # The earlier bill may already be in our books (then it must not be
        # expensed again) — reuse that payment if it is still unpaid.
        earlier = find_earlier(db, doc, prev, exclude_id=main_p.id)
        free = [p for p in earlier if p.voucher_id is None]
        if free:
            p = free[0]
            apply_bill_fields(p, pl)
            ids.append(p.id)
            notes.append(f"earlier bill {p.pay_no} RM {prev:,.2f} (already in books, paid together)")
        elif earlier:
            v = db.get(M.Voucher, earlier[0].voucher_id)
            notes.append(f"earlier RM {prev:,.2f} is already on {v.pv_no if v else 'a voucher'}"
                         f" ({earlier[0].pay_no}) — not paid again here")
        else:
            p = new_payment(prev, f"Unpaid balance from previous {doc.supplier} bill "
                                  f"(carried in {doc.invoice_no or doc.doc_no})")
            ids.append(p.id)
            notes.append(f"{p.pay_no} RM {prev:,.2f} unpaid earlier bill (was not in our books)")

    if not db.query(M.Supplier).filter(func.lower(M.Supplier.name) == doc.supplier.lower()).first():
        db.add(M.Supplier(name=doc.supplier,
                          sup_type="Supplier" if doc.section == "Purchase" else "Service Provider",
                          notes=f"Auto-created from {doc.doc_no} (Telegram)"))

    pl["bill_payments"] = ids
    doc.payload_json = json.dumps(pl)
    doc.payment_id = main_p.id
    doc.month = month
    doc.status, doc.verified_by, doc.verified_at = "Verified", who, datetime.utcnow()
    db.commit()
    ledger.sync_ledger(db)
    return True, " · ".join(notes)


def _open_listing(db: Session) -> M.Listing:
    """This month's bot listing that is still a draft, or a new one."""
    from .telegram_bot import next_monthly_counter
    ps = db.get(M.Setting, "PREFIX_PL")
    prefix = (ps.value.strip() if ps and ps.value.strip() else "PL-") + f"{date.today():%y%m}-"
    pl = (db.query(M.Listing)
          .filter(M.Listing.status == "Draft", M.Listing.pl_no.startswith(prefix),
                  M.Listing.prepared_by.endswith(TAG))
          .order_by(M.Listing.id.desc()).first())
    if pl:
        return pl
    pl = M.Listing(pl_no=next_monthly_counter(db, "PL", "PL-"), total=0.0,
                   prepared_by=f"Bills {TAG}")
    db.add(pl)
    db.flush()
    return pl


def raise_pv(db: Session, doc, who: str) -> tuple[bool, str, M.Listing | None]:
    from .main import issue_voucher, _refresh_listing

    ids = payload(doc).get("bill_payments") or ([doc.payment_id] if doc.payment_id else [])
    pays = db.query(M.Payment).filter(M.Payment.id.in_(ids)).all() if ids else []
    if doc.section == "Filing Only":
        return False, "Nothing to pay on this bill — it was filed, no voucher needed", None
    if doc.status != "Verified" or not pays:
        return False, "Verify the bill first", None
    done = [p for p in pays if p.voucher_id]
    if done:
        v = db.get(M.Voucher, done[0].voucher_id)
        pl = v.listing if v else None
        return False, f"Already on {v.pv_no}" + (f" / {pl.pl_no}" if pl else ""), pl
    pays = [p for p in pays if p.status in ("Unsorted", "Categorized")]
    if not pays:
        return False, "Nothing left to put on a voucher", None

    pv = issue_voucher(db, pays, doc.supplier, f"{who} {TAG}")
    pl = _open_listing(db)
    pv.listing_id = pl.id
    db.flush()
    db.refresh(pl)
    _refresh_listing(db, pl)
    db.commit()
    return True, f"{pv.pv_no} RM {pv.total:,.2f} → {pl.pl_no}", pl


# ───────────────────────── button handler ─────────────────────────
def _allowed(db: Session, from_id: str) -> bool:
    wl = db.get(M.Setting, "TELEGRAM_WHITELIST")
    wl = (wl.value if wl else "*").strip() or "*"
    if wl == "*":
        return True
    known = {u.telegram_id for u in db.query(M.User).filter(M.User.telegram_id != "").all()}
    return from_id in {x.strip() for x in wl.split(",")} | known


def handle_callback(db: Session, data: str, cq_id: str, frm: dict, chat_id, message_id):
    from .telegram_bot import tg_answer_callback, tg_edit, BASE_URL

    from_id = str(frm.get("id", ""))
    if not _allowed(db, from_id):
        tg_answer_callback(cq_id, f"⛔ Not authorised. Your Telegram ID: {from_id}", alert=True)
        return
    user = db.query(M.User).filter(M.User.telegram_id == from_id).first()
    who = (user.display_name if user else
           " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])) or from_id)
    if data.startswith("bill:sub:"):
        _submit(db, data, cq_id, who, chat_id, message_id)
        return
    try:
        _, action, did = data.split(":", 2)
        doc = db.get(M.Document, int(did))
    except ValueError:
        doc = None
    if not doc:
        tg_answer_callback(cq_id, "That document no longer exists", alert=True)
        return

    if action == "v":
        ok, msg = verify_bill(db, doc, who)
        tg_answer_callback(cq_id, "✅ Verified" if ok else msg, alert=not ok)
        if ok and chat_id and message_id:
            tg_edit(chat_id, message_id,
                    summary_text(doc).rsplit("\n\n", 1)[0]
                    + f"\n\n✅ *Verified by {_md(who)}* 已核实\n{_md(msg)}",
                    buttons=pv_buttons(doc) if doc.section != "Filing Only" else None)
    elif action == "pv":
        ok, msg, pl = raise_pv(db, doc, who)
        tg_answer_callback(cq_id, "📋 PV raised" if ok else msg, alert=not ok)
        if pl and chat_id and message_id:
            n = len(pl.vouchers)
            tg_edit(chat_id, message_id,
                    summary_text(doc).rsplit("\n\n", 1)[0]
                    + f"\n\n✅ Verified by {_md(doc.verified_by)}"
                    + f"\n📋 *{_md(msg)}*" * ok
                    + f"\n🗂 {pl.pl_no}: {n} voucher{'s' if n != 1 else ''} · RM {pl.total:,.2f} · {pl.status}",
                    buttons=listing_buttons(pl))
    else:
        tg_answer_callback(cq_id)


# ───────────────────────── /bills: re-post what is still open ─────────────────────────
NEW_KEYS = ("due_date", "jompay", "pay_to_account", "previous_balance", "amount_due")


def _refresh_reading(db: Session, doc) -> None:
    """Bills that arrived before this feature were read without the JomPAY /
    due-date fields. Read the stored file again and add ONLY those fields —
    supplier, amount etc. may already have been corrected by a person."""
    import os
    from . import claude_ai
    from .telegram_bot import UPLOAD_DIR
    pl = payload(doc)
    if "jompay" in pl or not doc.file_path:
        return
    path = os.path.join(UPLOAD_DIR, doc.file_path)
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        data = f.read()
    names = [s.name for s in db.query(M.Supplier).filter(M.Supplier.active == True).all()]  # noqa: E712
    cls = claude_ai.classify(data, doc.mime or "application/pdf", "", doc.file_path, supplier_names=names)
    if not cls.get("ai"):
        return
    for k in NEW_KEYS:
        pl[k] = cls.get(k)
    if not doc.doc_date and cls.get("date"):
        try:
            doc.doc_date = datetime.strptime(cls["date"], "%Y-%m-%d").date()
        except ValueError:
            pass
    doc.payload_json = json.dumps(pl)
    db.commit()


def open_bills(db: Session, days: int = 90) -> list:
    """Bills not yet on a voucher: waiting for Verify, or verified (on the web)
    but no PV raised yet."""
    from datetime import timedelta
    since = datetime.utcnow() - timedelta(days=days)
    out = []
    for doc in (db.query(M.Document)
                .filter(M.Document.section.in_(("Purchase", "Expense")),
                        M.Document.received_at >= since)
                .order_by(M.Document.id).all()):
        if doc.doc_type in M.PROVISIONAL_DOC_TYPES:
            continue
        if doc.status == "Pending" and (doc.amount or 0) > 0:
            out.append(doc)
        elif doc.status == "Verified" and doc.payment_id:
            p = db.get(M.Payment, doc.payment_id)
            if p and p.voucher_id is None and p.status in ("Unsorted", "Categorized"):
                out.append(doc)
    return out


def repost_open_bills(db: Session, chat_id) -> None:
    from .telegram_bot import tg_send
    docs = open_bills(db)
    if not docs:
        tg_send(chat_id, "✅ No bills waiting — everything is on a voucher.\n没有待处理的账单。")
        return
    tg_send(chat_id, f"🧾 *{len(docs)} bill{'s' if len(docs) != 1 else ''} waiting* 待处理账单 — "
                     "re-posted below with buttons.")
    for doc in docs:
        _refresh_reading(db, doc)
        pl = payload(doc)
        if doc.status == "Pending":
            tg_send(chat_id, summary_text(doc, db), buttons=verify_buttons(doc))
            continue
        # Verified on the web before: give its payment the JomPAY ref + due date
        for p in db.query(M.Payment).filter(M.Payment.id.in_(
                pl.get("bill_payments") or [doc.payment_id])).all():
            if not p.pay_ref:
                apply_bill_fields(p, pl)
        db.commit()
        tg_send(chat_id, summary_text(doc).rsplit("\n\n", 1)[0]
                + f"\n\n✅ Verified by {_md(doc.verified_by)} — tap to raise the PV.",
                buttons=pv_buttons(doc))


# ───────────────────────── Submit from Telegram ─────────────────────────
def listing_buttons(pl):
    from .telegram_bot import BASE_URL
    rows = []
    if pl.status == "Draft":
        rows.append([{"text": f"📤 Submit {pl.pl_no} for approval 提交审批",
                      "callback_data": f"bill:sub:{pl.id}"}])
    rows.append([{"text": f"🔗 Open {pl.pl_no}", "url": f"{BASE_URL}/listings"}])
    return rows


def submit_listing(db: Session, pl, who: str) -> tuple[bool, str, str]:
    """Draft → Submitted, plus a fresh approver link (older links revoked),
    exactly as the web Submit + 'Approver link' buttons do. Returns
    (ok, message, approver_url)."""
    import secrets
    from datetime import timedelta
    from .telegram_bot import BASE_URL
    from .main import SHARE_DEFAULT_DAYS
    if pl.status != "Draft":
        return False, f"{pl.pl_no} is already {pl.status.lower()}", ""
    if not pl.vouchers:
        return False, f"{pl.pl_no} has no vouchers", ""
    pl.status = "Submitted"
    for old in pl.shares:
        old.revoked = True
    sh = M.ListingShare(listing_id=pl.id, token=secrets.token_urlsafe(24),
                        created_by=f"{who} {TAG}",
                        expires_at=datetime.utcnow() + timedelta(days=SHARE_DEFAULT_DAYS))
    db.add(sh)
    db.commit()
    return True, f"{pl.pl_no} submitted", f"{BASE_URL}/approve/{sh.token}"


def listing_text(pl) -> str:
    lines = [f"📤 *{pl.pl_no} submitted for approval* 已提交审批", ""]
    for v in sorted(pl.vouchers, key=lambda v: v.pv_no):
        due = [p.due_date for p in v.payments if p.due_date]
        lines.append(f"• {v.pv_no} {_md(v.payee)} — *RM {v.total:,.2f}*"
                     + (f" · due {min(due):%d/%m}" if due else ""))
        if v.pay_ref:
            lines.append(f"   {_md(v.pay_ref)}")
    lines += ["", f"*Total RM {pl.total:,.2f}* · {len(pl.vouchers)} voucher"
                  + ("s" if len(pl.vouchers) != 1 else "")]
    return "\n".join(lines)


def _submit(db: Session, data: str, cq_id: str, who: str, chat_id, message_id):
    from .telegram_bot import tg_answer_callback, tg_edit_buttons, tg_send
    try:
        pl = db.get(M.Listing, int(data.rsplit(":", 1)[1]))
    except ValueError:
        pl = None
    if not pl:
        tg_answer_callback(cq_id, "That listing no longer exists", alert=True)
        return
    ok, msg, url = submit_listing(db, pl, who)
    tg_answer_callback(cq_id, "📤 Submitted" if ok else msg, alert=not ok)
    if not ok:
        return
    if chat_id and message_id:   # drop the Submit button from the message tapped
        tg_edit_buttons(chat_id, message_id, listing_buttons(pl))
    if chat_id:
        tg_send(chat_id, listing_text(pl) + f"\n📤 by {_md(who)}"
                "\n\nApprover link (forward to the approver, valid 14 days) 审批链接：",
                buttons=[[{"text": "✅ Open for approval 打开审批", "url": url}]])
        tg_send(chat_id, f"`{url}`")   # tap-to-copy; code format keeps _ in the token safe
