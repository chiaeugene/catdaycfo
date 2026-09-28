"""One-time data fixes that run during a deploy (called from seed.py).

Each backfill is keyed by a Setting and runs exactly once. They are written to
be safe against whatever the live data turned out to be: every step checks
before it writes, and anything unexpected is skipped and printed to the deploy
log rather than guessed at.
"""
from datetime import date, datetime

from . import models as M

# ─────────────────────────── AR invoice lines, Sept 2026 ───────────────────────────
# Invoices issued before invoices had lines were typed in as one amount with a
# one-line description, so the system's PDF didn't match the one the customer
# received. These are the lines exactly as printed on the invoices sent out.
# (description, qty, per, unit price, stream)
_PLAN = ("(6× Signature Cat Spa Ritual, 6× Premium Vacation Stay, "
         "6× Deflea & Deworming Care, 1× Final Health Check)")
_EXISTING = {
    "INV-2608-001": dict(lines=[
        ("Devon Rex Kitten (Male) — DOB 16 June 2025", 1, "", 6000.0, "Cat Sales")],
        notes=""),
    "INV-2609-001": dict(lines=[
        (f"Signature Cat Day Plan — Marvellous {_PLAN}", 1, "", 1899.0, "Membership"),
        (f"Signature Cat Day Plan — Kola {_PLAN}", 1, "", 1899.0, "Membership")],
        notes="Plan commenced 16/09/2026. Redeemed to date: Marvellous — 1× Signature Cat "
              "Spa Ritual; Kola — 1× Signature Cat Spa Ritual. Premium Vacation Stay "
              "excludes public holidays.",
        refs={3798.0: "408570XONV1M4"}),
    "INV-2609-002": dict(lines=[
        ("Comfort Recovery Plan — Kenzo", 1, "", 2099.0, "Membership"),
        ("Comfort Recovery Plan — Mei Mei", 1, "", 2099.0, "Membership")],
        notes="Checked in 16/09/2026."),
    "INV-2609-003": dict(lines=[
        ("The Haven boarding — Olaf, 14/08/2026 to 17/09/2026", 35, "night", 48.0, "Boarding"),
        ("Premium Grooming Trial — Olaf (17/09/2026)", 1, "", 99.0, "Grooming")],
        notes="Checked out 17/09/2026."),
    "INV-2609-004": dict(lines=[
        ("The Haven boarding — Grey Grey, 18/09/2026 to 20/09/2026", 2, "night", 88.0, "Boarding"),
        ("Premium Grooming — Grey Grey (19/09/2026)", 1, "", 99.0, "Grooming")],
        notes="Checked out 20/09/2026."),
    "INV-2609-005": dict(lines=[
        ("Premium Grooming — Elves Cutebb Ollie (18/09/2026)", 1, "", 99.0, "Grooming")],
        notes="", refs={99.0: "RPP20260923027190"}),
    "INV-2609-006": dict(lines=[
        ("The Big Nook boarding — Taro & Three, 17/09/2026 to 24/09/2026 "
         "(7 nights × RM88.00 opening rate)", 1, "", 616.0, "Boarding"),
        ("Founding Family Circle privilege rate — RM68.00/night (RM20.00 × 7 nights)",
         1, "", -140.0, "Boarding"),
        ("Premium Grooming — Taro & Three (23/09/2026), 2 × RM99.00", 1, "", 198.0, "Grooming")],
        notes="Founding Family Circle privilege: a special founding-customer rate, not the "
              "standard public rate. Includes a complimentary room upgrade from The Haven to "
              "The Big Nook, daily photo updates during the stay, complimentary basic wellness "
              "observation, and priority reservation for future peak periods. "
              "Check-out 24/09/2026."),
    "INV-2609-007": dict(lines=[
        ("Premium Grooming (RM99 promo) — Miya (24/09/2026)", 1, "", 99.0, "Grooming"),
        ("Premium Grooming (RM99 promo) — Miko (24/09/2026)", 1, "", 99.0, "Grooming")],
        notes=""),
}
# Requested by Karen on 27/09/2026 (her messages said 2027 — a typo).
_NEW = {
    "INV-2609-008": dict(customer="Milk Tan", contact="+60 12-313 2977", on=date(2026, 9, 27),
        lines=[("Premium Grooming — Casper & Luther, 27/09/2026", 2, "cat", 138.0, "Grooming")]),
    "INV-2609-009": dict(customer="Leng Zhi An", contact="+60 11-2001 0119", on=date(2026, 9, 27),
        lines=[("Premium Grooming (RM99 promo) — Simba, 27/09/2026", 1, "cat", 99.0, "Grooming")]),
    "INV-2609-010": dict(customer="Sherynn / Angel", contact="+60 12-260 2683", on=date(2026, 9, 27),
        lines=[("Premium Grooming — Haagen & Lucky, 27/09/2026", 2, "cat", 138.0, "Grooming")]),
}


def _lines(inv_id, spec):
    return [M.ARInvoiceLine(invoice_id=inv_id, description=d, qty=q, per=p,
                            unit_price=u, stream=s) for d, q, p, u, s in spec]


def _total(spec):
    return round(sum(round(q * u, 2) for _, q, _, u, _ in spec), 2)


def ar_lines_sept_2026(db):
    key = "BACKFILL_AR_LINES_2609"
    if db.get(M.Setting, key):
        return
    from .main import _build_invoice_pdf     # lazy: main imports half the app
    from . import ledger
    touched = []

    for no, spec in _EXISTING.items():
        inv = db.query(M.ARInvoice).filter(M.ARInvoice.inv_no == no).first()
        if not inv:
            print(f"  backfill: {no} not found — skipped")
            continue
        if inv.lines:
            print(f"  backfill: {no} already has lines — left alone")
            continue
        if abs(_total(spec["lines"]) - inv.amount) > 0.005:
            print(f"  backfill: {no} is RM{inv.amount:,.2f} in the system but RM"
                  f"{_total(spec['lines']):,.2f} on the invoice sent — NOT changed, check by hand")
            continue
        db.add_all(_lines(inv.id, spec["lines"]))
        inv.notes = spec["notes"]
        biggest = max(spec["lines"], key=lambda l: l[1] * l[3])
        inv.stream = biggest[4]
        for amt, ref in spec.get("refs", {}).items():
            for r in inv.receipts:
                if abs(r.amount - amt) < 0.005 and not r.notes:
                    r.notes = ref
        touched.append(inv)
        print(f"  backfill: {no} lines added ({len(spec['lines'])})")

    for no, spec in _NEW.items():
        if db.query(M.ARInvoice).filter(M.ARInvoice.inv_no == no).first():
            print(f"  backfill: {no} already exists — not created (check it is {spec['customer']})")
            continue
        amount = _total(spec["lines"])
        dup = db.query(M.ARInvoice).filter(M.ARInvoice.customer == spec["customer"],
                                           M.ARInvoice.date == spec["on"],
                                           M.ARInvoice.amount == amount).first()
        if dup:
            print(f"  backfill: {spec['customer']} already invoiced as {dup.inv_no} — not duplicated")
            continue
        biggest = max(spec["lines"], key=lambda l: l[1] * l[3])
        inv = M.ARInvoice(inv_no=no, customer=spec["customer"], cust_contact=spec["contact"],
                          cust_address="", stream=biggest[4], date=spec["on"],
                          due_date=spec["on"], amount=amount, month=f"{spec['on']:%b %Y}",
                          notes="", created_by="Eugene (for Karen)")
        db.add(inv)
        db.flush()
        db.add_all(_lines(inv.id, spec["lines"]))
        touched.append(inv)
        print(f"  backfill: {no} created — {spec['customer']} RM{amount:,.2f}")

    # The next invoice Karen creates must follow on, never reuse a number.
    c = db.get(M.Counter, "ARINV2609")
    top = max([int(n[-3:]) for (n,) in db.query(M.ARInvoice.inv_no)
               .filter(M.ARInvoice.inv_no.like("INV-2609-%")).all()] or [0])
    if c:
        c.value = max(c.value, top + 1)
    else:
        db.add(M.Counter(name="ARINV2609", value=top + 1))

    db.flush()
    for inv in touched:
        db.expire(inv, ["lines", "receipts"])
        try:
            inv.pdf_path = _build_invoice_pdf(db, inv)
        except Exception as e:
            print(f"  backfill: PDF for {inv.inv_no} failed: {e}")
    db.add(M.Setting(key=key, value=datetime.utcnow().isoformat(timespec="seconds")))
    db.commit()
    for inv in touched:
        ledger.repost(db, "ARInvoice", inv.id)
    print(f"  backfill: AR lines done — {len(touched)} invoices")


def attendance_roster_from_payroll(db):
    """Start the attendance list with everyone already on payroll, so Karen
    only has to add part-timers. Names only -- nothing from payroll is copied."""
    key = "BACKFILL_ATT_ROSTER"
    if db.get(M.Setting, key):
        return
    n = 0
    for s in db.query(M.Staff).filter(M.Staff.active == True).all():  # noqa: E712
        if not db.query(M.AttendancePerson).filter(M.AttendancePerson.staff_id == s.id).first():
            db.add(M.AttendancePerson(name=s.name, position=s.position or "",
                                      employment="Full-time", staff_id=s.id,
                                      created_by="system (from payroll)"))
            n += 1
    db.add(M.Setting(key=key, value=datetime.utcnow().isoformat(timespec="seconds")))
    db.commit()
    print(f"  backfill: attendance roster started with {n} payroll staff")


def run_all(db):
    ar_lines_sept_2026(db)
    attendance_roster_from_payroll(db)
