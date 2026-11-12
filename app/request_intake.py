"""Karen's Google-form requests → Purchase Requests, and approval by list.

Intake: an Apps Script in Karen's form spreadsheet posts every filled request
block here (all of them, each time — the push is idempotent). A block is keyed
"<spreadsheet id>:<block no>", so a re-send updates the same request instead of
creating another. Before a decision the request follows the form; after a
decision the form can no longer change it — the edit is recorded on the request
as a flag for finance instead.

Approval: Submitted requests are batched into a Request Listing (RL-yymm-nnn)
with a PDF in the cat day listing style and a long random link. Whoever opens
the link approves or rejects each request; no login, no name — the trail says
"via approval link" with the time. Same transitions as an approver in the
system (requests_logic), so an approved request is matched to its invoice later
exactly like any other.
"""
import json
import re
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from . import models as M
from . import telegram_bot, pdfgen
from .requests_logic import log_event, snapshot, build_pdf

LINK_DAYS = 14
VIA_LINK = "Approver (link)"


# ───────────────────────── small parsers ─────────────────────────
def money(v) -> float:
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(v or "").replace(" ", ""))
    try:
        return round(float(m.group(0).replace(",", "")), 2) if m else 0.0
    except ValueError:
        return 0.0


def day(v):
    s = str(v or "").strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def yymm_from(title: str, fallback: date | None) -> str:
    """'Copy of CatDay | Purchase & Expense Requests - Nov26' → '2611'."""
    m = re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s-]*'?(\d{2,4})", title or "", re.I)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)[:3].title()} {m.group(2)[-2:]}", "%b %y")
            return f"{d:%y%m}"
        except ValueError:
            pass
    return f"{(fallback or date.today()):%y%m}"


def intake_key(db: Session) -> str:
    """Shared secret the form's script sends. Made on first use, kept in Settings."""
    s = db.get(M.Setting, "REQUEST_INTAKE_KEY")
    if not s or not s.value.strip():
        s = s or M.Setting(key="REQUEST_INTAKE_KEY", value="")
        s.value = secrets.token_urlsafe(24)
        db.add(s)
        db.commit()
    return s.value


def _requester(db: Session, name: str):
    n = (name or "Karen").strip()
    u = db.query(M.User).filter(M.User.display_name.ilike(n)).first()
    return u, (u.display_name if u else n)


# ───────────────────────── intake ─────────────────────────
FIELDS = ("item", "why", "qty", "total", "supplier", "needed_by", "pay_method", "quote")


def _fingerprint(b: dict) -> dict:
    return {"item": " ".join(str(b.get("item") or "").split()),
            "why": " ".join(str(b.get("why") or "").split()),
            "qty": " ".join(str(b.get("qty") or "").split()),
            "total": money(b.get("total")),
            "supplier": " ".join(str(b.get("supplier") or "").split()),
            "needed_by": str(day(b.get("needed_by")) or ""),
            "pay_method": str(b.get("pay_method") or "").strip(),
            "quote": str(b.get("quote") or "").strip()}


def _current(pr) -> dict:
    ln = pr.lines[0] if pr.lines else None
    return {"item": ln.item if ln else "", "why": pr.reason, "qty": pr.qty_text, "total": pr.total,
            "supplier": pr.supplier, "needed_by": str(pr.needed_by or ""),
            "pay_method": pr.pay_method, "quote": pr.quote_ref}


def _write(pr, f: dict) -> None:
    pr.reason, pr.qty_text, pr.supplier = f["why"], f["qty"], f["supplier"]
    pr.needed_by = day(f["needed_by"]) if f["needed_by"] else None
    pr.pay_method, pr.quote_ref = f["pay_method"], f["quote"]
    pr.terms = "Cash before delivery" if "direct" in f["pay_method"].lower() else pr.terms
    if pr.lines:
        ln = pr.lines[0]
    else:
        ln = M.PurchaseRequestLine()
        pr.lines.append(ln)        # through the parent's collection, so it cascades into the session
    ln.item, ln.qty, ln.uom, ln.unit_price = (f["item"] or "(no item given)")[:150], 1.0, "lot", f["total"]
    ln.category = ln.category or "Misc"
    pr.total = f["total"]


def apply(db: Session, payload: dict) -> dict:
    sheet = str(payload.get("sheet_id") or "").strip()
    title = str(payload.get("sheet_title") or "")
    blocks = payload.get("requests") or []
    if not sheet:
        return {"ok": False, "error": "sheet_id missing"}
    out = {"ok": True, "new": [], "updated": [], "unchanged": 0, "flagged": [], "withdrawn": []}
    seen = set()
    for b in blocks:
        try:
            n = int(b.get("block"))
        except (TypeError, ValueError):
            continue
        f = _fingerprint(b)
        if not f["item"] or not f["total"]:
            continue                                   # empty or half-filled block — wait for it
        key = f"{sheet}:{n}"
        seen.add(key)
        pr = db.query(M.PurchaseRequest).filter(M.PurchaseRequest.source_key == key).first()
        if not pr:
            rd = day(b.get("request_date"))
            user, name = _requester(db, b.get("requester") or "Karen")
            pr = M.PurchaseRequest(
                source_key=key, form_ref=f"KR-{n:03d}",
                pr_no=f"KR-{yymm_from(title, rd)}-{n:03d}",
                requester_id=user.id if user else None, requester=name,
                purpose="Restock", urgency="Normal", status="Submitted",
                created_at=datetime.combine(rd, datetime.min.time()) if rd else datetime.utcnow(),
                submitted_at=datetime.utcnow())
            db.add(pr)
            _write(pr, f)
            db.flush()
            log_event(db, pr, name, "submitted",
                      f"From Karen's form ({title or 'request form'}, block {n:02d}) · RM {pr.total:,.2f}",
                      "form")
            build_pdf(db, pr)
            out["new"].append(pr.pr_no)
            continue
        cur = _current(pr)
        diff = {k: (cur[k], f[k]) for k in FIELDS if str(cur[k]) != str(f[k])}
        if not diff:
            out["unchanged"] += 1
            continue
        text = "; ".join(f"{k}: '{a}' → '{b}'" for k, (a, b) in diff.items())
        if pr.status in ("Submitted", "Draft", "Returned"):
            _write(pr, f)
            log_event(db, pr, pr.requester, "updated", f"Form edited · {text}", "form")
            build_pdf(db, pr)
            if pr.rlist:
                build_list_pdf(db, pr.rlist)
            out["updated"].append(pr.pr_no)
        else:
            # Decided already: the form can't rewrite what was approved/rejected.
            if text not in (pr.form_changed or ""):
                pr.form_changed = ((pr.form_changed + "\n") if pr.form_changed else "") + \
                    f"{datetime.utcnow() + timedelta(hours=8):%d/%m %H:%M} {text}"
                log_event(db, pr, pr.requester, "form changed",
                          f"Form edited after {pr.status.lower()} — not applied · {text}", "form")
                out["flagged"].append(pr.pr_no)
    # A block cleared on the form before any decision = withdrawn.
    for pr in db.query(M.PurchaseRequest).filter(M.PurchaseRequest.source_key.like(f"{sheet}:%"),
                                                 M.PurchaseRequest.status == "Submitted").all():
        if pr.source_key not in seen and blocks is not None and payload.get("complete", True):
            pr.status = "Cancelled"
            log_event(db, pr, pr.requester, "withdrawn", "Cleared on Karen's form before a decision", "form")
            out["withdrawn"].append(pr.pr_no)
    db.commit()
    return out


# ───────────────────────── request listings ─────────────────────────
def build_list_pdf(db: Session, rl: M.RequestList) -> str:
    settings = {s.key: s.value for s in db.query(M.Setting).all()}
    rows = [{"no": r.pr_no, "form_ref": r.form_ref,
             "date": f"{(r.created_at or r.submitted_at):%d/%m/%y}",
             "item": (r.lines[0].item if r.lines else ""), "qty": r.qty_text, "why": r.reason,
             "supplier": r.supplier, "quote": r.quote_ref,
             "needed": f"{r.needed_by:%d/%m/%y}" if r.needed_by else "",
             "total": r.total, "status": r.status,
             "decided": (f"{r.decided_at + timedelta(hours=8):%d/%m %H:%M}" if r.decided_at else ""),
             "note": r.decision_note} for r in rl.requests]
    rl.pdf_path = pdfgen.request_list_pdf(
        rl.rl_no, rows, rl.total,
        company=settings.get("COMPANY_NAME", "CATDAY SDN BHD"),
        address=settings.get("COMPANY_ADDRESS", "Uptown PJ"),
        reg_no=settings.get("COMPANY_ROC", ""))
    return rl.pdf_path


def create_list(db: Session, ids: list[int], who: str) -> tuple[M.RequestList | None, str]:
    prs = db.query(M.PurchaseRequest).filter(M.PurchaseRequest.id.in_(ids),
                                             M.PurchaseRequest.status == "Submitted").all()
    prs = [p for p in prs if not (p.rlist and p.rlist.live and p in p.rlist.pending)]
    if not prs:
        return None, "Nothing to list — tick requests that are waiting for approval and not already on an open list."
    rl = M.RequestList(rl_no=telegram_bot.next_monthly_counter(db, "RL", "RL-"),
                       created_by=who, token=secrets.token_urlsafe(24),
                       expires_at=datetime.utcnow() + timedelta(days=LINK_DAYS))
    db.add(rl)
    db.flush()
    for p in prs:
        p.rlist_id = rl.id
        log_event(db, p, who, "listed", f"Put on {rl.rl_no} for approval")
    db.flush()
    db.refresh(rl)
    build_list_pdf(db, rl)
    db.commit()
    return rl, f"{rl.rl_no} created — {len(prs)} request{'s' if len(prs) != 1 else ''}, RM {rl.total:,.2f}."


def message(rl: M.RequestList, url: str) -> str:
    """The text to paste into WhatsApp / Telegram with the link."""
    rs = sorted(rl.requests, key=lambda r: (r.needed_by or date.max, r.pr_no))
    lines = [f"Hi, {len(rs)} purchase request{'s' if len(rs) != 1 else ''} for approval "
             f"({rl.rl_no}), total RM {rl.total:,.2f}:", ""]
    for r in rs:
        item = r.lines[0].item if r.lines else ""
        lines.append(f"• {r.form_ref or r.pr_no} {item[:60]} — {r.supplier or '-'} — RM {r.total:,.2f}"
                     + (f" (needed by {r.needed_by:%d %b})" if r.needed_by else ""))
    lines += ["", "Please approve or reject each one here:", url]
    return "\n".join(lines)


# ───────────────────────── decisions from the link ─────────────────────────
def decide_by_link(db: Session, rl: M.RequestList, pr_id: int, action: str, note: str = "") -> tuple[bool, str]:
    pr = db.get(M.PurchaseRequest, pr_id)
    if not pr or pr.rlist_id != rl.id:
        return False, "That request isn't on this list."
    if not rl.live:
        return False, "This approval link has expired or been withdrawn — ask finance for a new one."
    if pr.status != "Submitted":
        return False, f"{pr.form_ref or pr.pr_no} was already {pr.status.lower()}."
    note = " ".join((note or "").split())[:500]
    if action == "reject" and not note:
        return False, "Please give a reason for rejecting — Karen needs to know what to change."
    now = datetime.utcnow()
    if action == "approve":
        pr.status, pr.decided_by, pr.decided_at, pr.decision_note = "Approved", VIA_LINK, now, note
        pr.expires_at = date.today() + timedelta(days=M.PR_APPROVAL_DAYS)
        pr.approved_json = snapshot(pr)
        log_event(db, pr, VIA_LINK, "approved", f"RM {pr.total:,.2f} on {rl.rl_no}"
                  + (f" · {note}" if note else ""), "link")
    elif action == "reject":
        pr.status, pr.decided_by, pr.decided_at, pr.decision_note = "Rejected", VIA_LINK, now, note
        log_event(db, pr, VIA_LINK, "rejected", f"On {rl.rl_no} · {note}", "link")
    else:
        return False, "Unknown action."
    build_pdf(db, pr)
    build_list_pdf(db, rl)
    db.commit()
    return True, f"{pr.form_ref or pr.pr_no} {pr.status.lower()}."
