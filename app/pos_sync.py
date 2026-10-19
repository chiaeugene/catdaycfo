"""Pull sales from the POS (crm.catday.pet) into the Sales Ledger.

The POS is where sales now happen; this system is where the money is
accounted for. Every few hours the sync signs in to the POS with its own
read-only account, reads the invoices and payments, and for each paid
invoice does exactly one of:

  matched  -- the sale is already here (a walk-in line from a daily report,
              or one of our own invoices): link it, enter nothing new.
  posted   -- not here yet: enter it as a sale, by type and payment method.
  review   -- something a person should decide (paid in the POS but still
              owed in Receivables, unpaid in the POS, more than one candidate).
  voided   -- voided in the POS: anything we posted from it is taken out.

Each POS invoice is keyed by its POS id, so running the sync again never
enters a sale twice. Nothing is ever written to the POS.

Credentials come from the environment (POS_EMAIL / POS_PASSWORD), set in
Render by the owner. They are never stored in the database or shown on a page.
"""
import hashlib
import json
import os
import re
import threading
from datetime import date, datetime, timedelta

import httpx

from . import models as M

BASE = os.environ.get("POS_BASE_URL", "https://crm.catday.pet").rstrip("/")
SYNC_HOURS = float(os.environ.get("POS_SYNC_HOURS", "3"))
# POS invoices dated before this are history the daily reports already
# covered by other means; they are matched if possible but never posted.
DEFAULT_FROM = "2026-09-01"
_LOCK = threading.Lock()


def configured() -> bool:
    return bool(os.environ.get("POS_EMAIL") and os.environ.get("POS_PASSWORD"))


# ─────────────────────────── reading the POS ───────────────────────────
def fetch(client: httpx.Client | None = None) -> dict:
    """Sign in and read invoices + payments. Raises on any failure, with a
    message fit for the status line (never containing the password)."""
    own = client is None
    client = client or httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    try:
        r = client.post("/api/auth/login", json={"email": os.environ.get("POS_EMAIL", ""),
                                                "password": os.environ.get("POS_PASSWORD", "")})
        if r.status_code != 200:
            raise RuntimeError(f"POS sign-in refused (HTTP {r.status_code}) — check POS_EMAIL / "
                               "POS_PASSWORD in Render, and that the account is still active.")
        out = {}
        for key, path in (("invoices", "/api/invoices"), ("payments", "/api/payments")):
            r = client.get(path)
            if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                raise RuntimeError(f"POS {path} answered HTTP {r.status_code} — the POS may have "
                                   "changed; the sync needs a look.")
            data = r.json()
            out[key] = data if isinstance(data, list) else (data.get("data") or data.get("items") or [])
        try:
            client.post("/api/auth/logout")
        except httpx.HTTPError:
            pass
        return out
    except httpx.HTTPError as e:
        raise RuntimeError(f"Could not reach the POS ({type(e).__name__}).") from None
    finally:
        if own:
            client.close()


# ─────────────────────────── interpreting it ───────────────────────────
def _num(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _myt_date(iso: str) -> date:
    """POS times are UTC; the sale belongs to the Malaysian calendar day."""
    try:
        t = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return (t + timedelta(hours=8)).date()
    except ValueError:
        return date.today()


_PREPAID = ("package", "plan", "membership", "prepaid", "voucher")


def stream_for(line: dict, source: str) -> str:
    """Which of our sales types a POS line is. Prepaid packages are money
    received for sessions still to be done, so they are Membership, not
    Grooming — the POS counts them as Grooming, which overstates the month."""
    desc = (line.get("description") or "").lower()
    qty = _num(line.get("quantity")) or 1
    if line.get("hotelBookingId") or source == "HOTEL_BOOKING" or \
            any(w in desc for w in ("stay", "boarding", "night", "haven", "nook", "penthouse")):
        return "Boarding"
    if any(w in desc for w in _PREPAID) or re.match(r"\s*\d+\s*x\b", desc) \
            or (qty >= 5 and "session" in desc):
        return "Membership"
    if line.get("productId"):
        return "Retail"
    return "Grooming"


def method_for(name: str) -> str:
    n = (name or "").lower()
    if "cash" in n:
        return "Cash"
    if any(w in n for w in ("card", "credit", "debit", "visa", "master")):
        return "Card"
    if any(w in n for w in ("tng", "touch", "ewallet", "e-wallet", "grab", "boost")):
        return "TNG"
    if "cheque" in n:
        return "Cheque"
    return "Bank Transfer"      # DuitNow / QR / transfer all land in the bank


def summarise(inv: dict, payments: list) -> dict:
    lines = inv.get("lineItems") or []
    streams = {}
    for ln in lines:
        st = stream_for(ln, inv.get("source") or "")
        streams[st] = round(streams.get(st, 0) + _num(ln.get("lineTotal")), 2)
    total = _num(inv.get("totalAmount"))
    # Invoice-level discount, fee or tax: the lines don't add to the total.
    # Put the difference on the biggest type so the sale equals what was paid.
    if streams and abs(sum(streams.values()) - total) > 0.005:
        big = max(streams, key=streams.get)
        streams[big] = round(streams[big] + total - sum(streams.values()), 2)
    if not streams:
        streams = {"Grooming": total}
    pays = [p for p in payments if p.get("invoiceId") == inv.get("id")
            and (p.get("status") or "").upper() == "COMPLETED"
            and (p.get("purpose") or "PAYMENT").upper() != "REFUND"]
    by_method = {}
    for p in pays:
        by_method[p.get("methodName") or "?"] = by_method.get(p.get("methodName") or "?", 0) + _num(p.get("amount"))
    main = max(by_method, key=by_method.get) if by_method else ""
    items = "; ".join(f"{(ln.get('description') or '').strip()}"
                      + (f" ×{_num(ln.get('quantity')):g}" if _num(ln.get("quantity")) > 1 else "")
                      for ln in lines)
    out = {
        "pos_id": inv.get("id"), "invoice_no": inv.get("invoiceNo") or "",
        "date": _myt_date(inv.get("issueDate")),
        "customer": ((inv.get("customer") or {}).get("fullName") or "").strip(),
        "cat": ((inv.get("cat") or {}) or {}).get("name") or "",
        "status": (inv.get("status") or "").upper(), "total": total,
        "paid": _num(inv.get("paidAmount")),
        "method": " + ".join(f"{m} {a:,.2f}" for m, a in by_method.items()) if len(by_method) > 1 else main,
        "main_method": method_for(main),
        "reference": ", ".join(p.get("referenceNo") or "" for p in pays if p.get("referenceNo")),
        "paid_on": _myt_date(pays[0].get("paidAt")) if pays else None,
        "items": items, "streams": streams,
    }
    out["sig"] = hashlib.sha1(json.dumps(
        [out["status"], out["total"], out["paid"], out["method"], sorted(streams.items()), str(out["date"])],
        default=str).encode()).hexdigest()
    return out


# ─────────────────────────── applying it ───────────────────────────
def _words(name: str) -> set:
    return {w for w in "".join(c if c.isalnum() else " " for c in (name or "").lower()).split()
            if len(w) >= 3}


def _find_walkin(db, s: dict):
    lo, hi = s["date"] - timedelta(days=1), s["date"] + timedelta(days=1)
    rows = db.query(M.SalesEntry).filter(M.SalesEntry.date >= lo, M.SalesEntry.date <= hi,
                                         M.SalesEntry.pos_ref == "").all()
    return [r for r in rows if abs(r.amount - s["total"]) < 0.005]


def _find_invoice(db, s: dict):
    lo, hi = s["date"] - timedelta(days=3), s["date"] + timedelta(days=3)
    taken = {i for (i,) in db.query(M.PosInvoice.ar_invoice_id)
             .filter(M.PosInvoice.ar_invoice_id.isnot(None), M.PosInvoice.pos_id != s["pos_id"])}
    cands = []
    for inv in db.query(M.ARInvoice).filter(M.ARInvoice.date >= lo, M.ARInvoice.date <= hi,
                                            M.ARInvoice.status != "Void").all():
        if inv.id in taken or abs(inv.amount - s["total"]) > 0.005:
            continue
        if _words(inv.customer) & (_words(s["customer"]) | _words(s["cat"])):
            cands.append(inv)
    return cands


def _post(db, s: dict) -> list:
    rows = []
    for stream, amt in s["streams"].items():
        if not amt:
            continue
        e = M.SalesEntry(date=s["date"], stream=stream, amount=amt, method=s["main_method"],
                         description=f"POS {s['invoice_no']} — {s['customer']}"
                                     + (f" / {s['cat']}" if s["cat"] else "") + f": {s['items']}"[:400],
                         month=f"{s['date']:%b %Y}", recorded_by="POS sync", pos_ref=s["pos_id"])
        db.add(e)
        rows.append(e)
    return rows


def _unpost(db, pos_id: str) -> int:
    """Take out sales this sync entered for a POS invoice (never ones a person
    entered — those were matched, and are only unlinked)."""
    n = 0
    for e in db.query(M.SalesEntry).filter(M.SalesEntry.pos_ref == pos_id).all():
        if e.recorded_by == "POS sync":
            db.delete(e)
            n += 1
        else:
            e.pos_ref = ""
    return n


def apply(db, data: dict, sync_from: date) -> dict:
    counts = {"new": 0, "posted": 0, "matched": 0, "review": 0, "voided": 0, "unchanged": 0}
    for inv in data.get("invoices") or []:
        s = summarise(inv, data.get("payments") or [])
        if not s["pos_id"]:
            continue
        rec = db.query(M.PosInvoice).filter(M.PosInvoice.pos_id == s["pos_id"]).first()
        if rec and rec.sig == s["sig"]:
            rec.synced_at = datetime.utcnow()
            counts["unchanged"] += 1
            continue
        if not rec:
            rec = M.PosInvoice(pos_id=s["pos_id"])
            db.add(rec)
            counts["new"] += 1
        elif rec.outcome in ("posted", "matched", "review"):
            # Changed in the POS since last time: undo what we did, decide again.
            _unpost(db, s["pos_id"])
            rec.ar_invoice_id = None
        for k in ("invoice_no", "date", "customer", "cat", "status", "total", "paid", "method",
                  "reference", "items", "sig"):
            setattr(rec, k, s[k])
        rec.streams_json = json.dumps(s["streams"])
        rec.synced_at = datetime.utcnow()
        db.flush()
        outcome, note = _decide(db, s, rec, sync_from)
        rec.outcome, rec.note = outcome, note
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _decide(db, s: dict, rec, sync_from: date):
    if s["status"] in ("VOID", "VOIDED", "CANCELLED", "REFUNDED"):
        return "voided", f"{s['status'].title()} in the POS — not a sale."
    if s["status"] != "PAID" or s["paid"] + 0.005 < s["total"]:
        return "review", (f"Not fully paid in the POS (RM{s['paid']:,.2f} of RM{s['total']:,.2f}). "
                          "If the customer owes the rest, raise it in Receivables.")
    walk = _find_walkin(db, s)
    if len(walk) == 1:
        walk[0].pos_ref = s["pos_id"]
        return "matched", f"Already in Sales from a daily report ({walk[0].date:%d/%m}, RM{walk[0].amount:,.2f})."
    invs = _find_invoice(db, s)
    if len(invs) == 1:
        inv = invs[0]
        rec.ar_invoice_id = inv.id
        if inv.outstanding > 0.005:
            return "review", (f"Same sale as {inv.inv_no}, which still shows RM{inv.outstanding:,.2f} owed. "
                              f"The POS says it was paid {s['paid_on']:%d/%m/%Y} by {s['method']}"
                              + (f", ref {s['reference']}" if s["reference"] else "")
                              + " — record the receipt on that invoice.")
        return "matched", f"Same sale as {inv.inv_no} (already paid here)."
    if len(walk) + len(invs) > 1:
        return "review", ("More than one record here could be this sale: "
                          + ", ".join([f"sale {w.date:%d/%m} RM{w.amount:,.2f}" for w in walk]
                                      + [i.inv_no for i in invs]) + ". Decide which, or post it.")
    if s["date"] < sync_from:
        return "skipped", f"Before {sync_from:%d/%m/%Y}, when POS syncing starts — not entered."
    _post(db, s)
    return "posted", "Entered in Sales: " + ", ".join(f"{k} RM{v:,.2f}" for k, v in s["streams"].items()) \
        + f" · {s['main_method']}."


# ─────────────────────────── running it ───────────────────────────
def _setting(db, key, default=""):
    row = db.get(M.Setting, key)
    return row.value if row else default


def _set(db, key, value):
    row = db.get(M.Setting, key)
    if row:
        row.value = value
    else:
        db.add(M.Setting(key=key, value=value))


def run(db, data: dict | None = None, who: str = "auto") -> dict:
    """One sync. `data` lets tests (and a manual import) skip the network."""
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "error": "A sync is already running."}
    try:
        try:
            sync_from = date.fromisoformat(_setting(db, "POS_SYNC_FROM", DEFAULT_FROM))
        except ValueError:
            sync_from = date.fromisoformat(DEFAULT_FROM)
        try:
            if data is None:
                if not configured():
                    raise RuntimeError("Not set up: POS_EMAIL and POS_PASSWORD are not set in Render.")
                data = fetch()
            counts = apply(db, data, sync_from)
            status = {"ok": True, "at": datetime.utcnow().isoformat(timespec="seconds"), "by": who,
                      "seen": len(data.get("invoices") or []), **counts}
            db.commit()
        except Exception as e:      # noqa: BLE001 — reported on the page, never crashes the app
            db.rollback()
            status = {"ok": False, "at": datetime.utcnow().isoformat(timespec="seconds"), "by": who,
                      "error": str(e)[:300]}
        _set(db, "POS_LAST_SYNC", json.dumps(status))
        db.commit()
        return status
    finally:
        _LOCK.release()


def last_status(db) -> dict:
    try:
        return json.loads(_setting(db, "POS_LAST_SYNC", "{}") or "{}")
    except ValueError:
        return {}


def start_scheduler(session_factory) -> None:
    """Background thread, like the backup scheduler: Render's plan has no cron.
    First run shortly after start-up, then every POS_SYNC_HOURS."""
    if not configured():
        return

    def loop():
        import time
        time.sleep(90)
        while True:
            db = session_factory()
            try:
                run(db, who="auto")
            except Exception:       # noqa: BLE001
                pass
            finally:
                db.close()
            time.sleep(SYNC_HOURS * 3600)

    threading.Thread(target=loop, daemon=True, name="pos-sync").start()
