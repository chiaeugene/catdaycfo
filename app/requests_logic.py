"""Purchase requests: the rules, in one place.

Both the web routes and the Telegram button handler call into here, so a
decision made by tapping a button in a group obeys exactly the same rules as
one made on the page — you can't approve your own request from either, a
Returned request can't be approved from either, and both leave the same trail.

Nothing in this module touches the ledger. A request is a permission, not a
transaction: the invoice that arrives later still goes through verification,
payment and voucher like any other.
"""
import json
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from . import models as M
from . import telegram_bot, pdfgen


# ─────────────────────────── helpers ───────────────────────────
def log_event(db: Session, pr: M.PurchaseRequest, who: str, action: str,
              detail: str = "", channel: str = "web") -> None:
    db.add(M.PurchaseRequestEvent(request_id=pr.id, who=who, action=action,
                                  detail=detail, channel=channel))


def recompute(pr: M.PurchaseRequest) -> None:
    """Re-total from the lines as they are in the session right now. Lines are
    deleted and re-added on every edit, and the relationship collection does
    not notice that on its own -- so expire it first, or an edit that doubles
    the quantity keeps the old total."""
    from sqlalchemy.orm import object_session
    sess = object_session(pr)
    if sess is not None:
        sess.flush()
        sess.expire(pr, ["lines"])
    pr.total = round(sum(ln.amount for ln in pr.lines), 2)


def snapshot(pr: M.PurchaseRequest) -> str:
    """What was approved, frozen. Any later edit cancels the approval, so this
    only ever differs from the live lines in the history."""
    return json.dumps({
        "supplier": pr.supplier, "purpose": pr.purpose, "reason": pr.reason,
        "terms": pr.terms, "total": pr.total,
        "lines": [{"item": l.item, "category": l.category, "qty": l.qty,
                   "uom": l.uom, "unit_price": l.unit_price, "amount": l.amount}
                  for l in pr.lines],
    }, ensure_ascii=False)


def describe_lines(pr: M.PurchaseRequest, limit: int = 4) -> str:
    """'3 box Royal Canin Kitten, 2 bottle Shampoo 5L, +1 more'"""
    parts = []
    for ln in pr.lines[:limit]:
        q = f"{ln.qty:g}" if float(ln.qty).is_integer() else f"{ln.qty:,.2f}"
        parts.append(f"{q} {ln.uom} {ln.item}")
    if len(pr.lines) > limit:
        parts.append(f"+{len(pr.lines) - limit} more")
    return ", ".join(parts)


def build_pdf(db: Session, pr: M.PurchaseRequest) -> str:
    settings = {s.key: s.value for s in db.query(M.Setting).all()}
    req = {
        "requester": pr.requester,
        "date": f"{(pr.submitted_at or pr.created_at):%d/%m/%Y}",
        "needed_by": f"{pr.needed_by:%d/%m/%Y}" if pr.needed_by else "",
        "urgency": pr.urgency, "supplier": pr.supplier, "purpose": pr.purpose,
        "reason": pr.reason, "terms": pr.terms,
        "decided_by": pr.decided_by,
        "decided_at": f"{pr.decided_at:%d/%m/%Y %H:%M}" if pr.decided_at else "",
        "expires": f"{pr.expires_at:%d/%m/%Y}" if pr.expires_at else "",
        "note": pr.decision_note,
    }
    lines = [{"item": l.item, "category": l.category, "qty": l.qty, "uom": l.uom,
              "unit_price": l.unit_price, "amount": l.amount} for l in pr.lines]
    pr.pdf_path = pdfgen.request_pdf(
        pr.pr_no or "DRAFT", req, lines, pr.total,
        company=settings.get("COMPANY_NAME", "CATDAY SDN BHD"),
        address=settings.get("COMPANY_ADDRESS", "Uptown PJ"),
        reg_no=settings.get("COMPANY_ROC", ""),
        approved=(pr.status == "Approved"))
    return pr.pdf_path


# ─────────────────────────── notifications ───────────────────────────
def _approvers(db: Session):
    return db.query(M.User).filter(M.User.can_approve == True,  # noqa: E712
                                   M.User.active == True,       # noqa: E712
                                   M.User.telegram_id != "").all()


def notify_approvers(db: Session, pr: M.PurchaseRequest) -> int:
    """DM every approver who has linked Telegram. Returns how many were told."""
    urgent = "🔴 *URGENT* — " if pr.urgency == "Urgent" else ""
    need = f"\nNeeded by: {pr.needed_by:%d/%m}" if pr.needed_by else ""
    text = (f"{urgent}🛒 *Purchase request {pr.pr_no}*\n"
            f"From: {pr.requester}\n"
            f"Supplier: {pr.supplier or '-'}"
            + (" _(new — not on file)_" if pr.supplier_new else "") + "\n"
            f"For: {pr.purpose} — {pr.reason[:120]}\n"
            f"Items: {describe_lines(pr)}\n"
            f"*Total: RM {pr.total:,.2f}*{need}")
    n = 0
    for u in _approvers(db):
        if u.id == pr.requester_id:
            continue          # you don't get asked to approve your own
        try:
            telegram_bot.tg_send(u.telegram_id, text, telegram_bot.request_buttons(pr.id))
            n += 1
        except Exception:
            pass
    return n


def notify_requester(db: Session, pr: M.PurchaseRequest, text: str) -> None:
    u = db.get(M.User, pr.requester_id) if pr.requester_id else None
    if not u or not u.telegram_id:
        return
    try:
        telegram_bot.tg_send(
            u.telegram_id, text,
            [[{"text": "🔗 Open request", "url": f"{telegram_bot.BASE_URL}/requests/{pr.id}"}]])
    except Exception:
        pass


# ─────────────────────────── transitions ───────────────────────────
def submit_request(db: Session, pr: M.PurchaseRequest, user: M.User) -> tuple[bool, str]:
    if pr.status not in ("Draft", "Returned"):
        return False, f"{pr.pr_no or 'This request'} is {pr.status} — it can't be submitted."
    if not pr.lines:
        return False, "Add at least one item before submitting."
    if not pr.reason.strip():
        return False, "Say why it's needed — the approver reads that line first."
    recompute(pr)
    resubmit = pr.status == "Returned"
    if not pr.pr_no:
        pr.pr_no = telegram_bot.next_monthly_counter(db, "PR", "PR-")
    pr.status = "Submitted"
    pr.submitted_at = datetime.utcnow()
    pr.decided_by, pr.decided_at, pr.decision_note = "", None, ""
    log_event(db, pr, user.display_name, "resubmitted" if resubmit else "submitted",
              f"RM {pr.total:,.2f} · {describe_lines(pr)}")
    build_pdf(db, pr)
    db.commit()
    told = notify_approvers(db, pr)
    return True, (f"{pr.pr_no} submitted — RM {pr.total:,.2f}. "
                  + (f"{told} approver{'s' if told != 1 else ''} pinged on Telegram."
                     if told else "No approver has Telegram linked yet — they'll see it here."))


def decide_request(db: Session, pr_id: int, action: str, user: M.User,
                   note: str = "", channel: str = "web") -> tuple[bool, str]:
    """approve / return / reject. Returns (ok, message for the person acting)."""
    pr = db.get(M.PurchaseRequest, pr_id)
    if not pr:
        return False, "Request not found."
    if not user.can_approve:
        return False, "You're not set as an approver."
    if pr.requester_id == user.id:
        return False, "You can't approve your own request — another approver has to."
    if pr.status != "Submitted":
        return False, f"{pr.pr_no} is {pr.status}, not waiting for a decision."
    note = (note or "").strip()
    if action in ("return", "reject") and not note:
        return False, "Give a reason — the requester needs to know what to change."

    who = user.display_name
    if action == "approve":
        pr.status = "Approved"
        pr.decided_by, pr.decided_at, pr.decision_note = who, datetime.utcnow(), note
        pr.expires_at = date.today() + timedelta(days=M.PR_APPROVAL_DAYS)
        pr.approved_json = snapshot(pr)
        log_event(db, pr, who, "approved",
                  f"RM {pr.total:,.2f}" + (f" · {note}" if note else ""), channel)
        build_pdf(db, pr)
        db.commit()
        notify_requester(db, pr, f"✅ *{pr.pr_no} approved* by {who} — free to go.\n"
                                 f"RM {pr.total:,.2f} · {pr.supplier or '-'}\n"
                                 f"Valid until {pr.expires_at:%d/%m/%Y}."
                                 + (f"\nNote: {note}" if note else ""))
        return True, f"{pr.pr_no} approved."
    if action == "return":
        pr.status = "Returned"
        pr.decided_by, pr.decided_at, pr.decision_note = who, datetime.utcnow(), note
        log_event(db, pr, who, "returned", note, channel)
        db.commit()
        notify_requester(db, pr, f"↩ *{pr.pr_no} returned* by {who}:\n_{note}_\n"
                                 f"Fix it and resubmit — same number.")
        return True, f"{pr.pr_no} returned to {pr.requester}."
    if action == "reject":
        pr.status = "Rejected"
        pr.decided_by, pr.decided_at, pr.decision_note = who, datetime.utcnow(), note
        log_event(db, pr, who, "rejected", note, channel)
        db.commit()
        notify_requester(db, pr, f"⛔ *{pr.pr_no} rejected* by {who}:\n_{note}_")
        return True, f"{pr.pr_no} rejected."
    return False, "Unknown action."


def unapprove_on_edit(db: Session, pr: M.PurchaseRequest, who: str, what: str) -> None:
    """An approved request that gets edited is no longer the thing that was
    approved. Back to Draft, approval wiped, trail says why."""
    pr.status = "Draft"
    pr.decided_by, pr.decided_at, pr.decision_note = "", None, ""
    pr.expires_at, pr.approved_json = None, ""
    log_event(db, pr, who, "unapproved", f"Edited after approval ({what}) — must be re-approved")


# ─────────────────────────── matching to payments ───────────────────────────
def approved_for_supplier(db: Session, supplier: str):
    """Approved requests worth offering when an invoice from this supplier is
    verified — same supplier first, then everything else still open."""
    q = db.query(M.PurchaseRequest).filter(M.PurchaseRequest.status == "Approved") \
        .order_by(M.PurchaseRequest.id.desc())
    allp = q.all()
    sl = (supplier or "").strip().lower()
    same = [p for p in allp if p.supplier.strip().lower() == sl] if sl else []
    rest = [p for p in allp if p not in same]
    return same, rest


def payment_flag(p: M.Payment) -> str:
    """'' if fine; otherwise a short reason this payment carries a flag."""
    if p.status == "Void":
        return ""
    if p.request_id and p.request:
        pr = p.request
        if pr.total and p.amount > pr.total * 1.10 + 1:
            return f"Over approval: RM {p.amount:,.2f} vs approved RM {pr.total:,.2f}"
        return ""
    if p.category in M.APPROVAL_EXEMPT:
        return ""
    return "No approval"
