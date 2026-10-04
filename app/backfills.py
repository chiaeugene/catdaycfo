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


# ─────────────────────────── Karen's Aug & Sep 2026 claims ───────────────────────────
# Ms Lee kept these two lists in Excel because the system had nowhere to put a
# monthly claim. Loaded as DRAFTS exactly as she wrote them, so she reviews
# rather than retypes 65 lines. A draft posts nothing.
# (dd/mm/yyyy, paid to, what for, amount)
_CLAIM_AUG = [
    ("03/08/2026", "FORTUNE ELECTRICAL AND HARDWARE", "PENDANT LIGHT BULB REPLACEMENT (SIGNATURE ROOM)", 15.00),
    ("07/08/2026", "FORTUNE ELECTRICAL AND HARDWARE", "TAPE FOR PACKING EQUIPMENTS/ ITEMS TO MOVE FROM USJ TO DAMANSARA UTAMA", 9.00),
    ("07/08/2026", "HOUSES LIGHTINGS SDN BHD", "TRACK + TRACK LIGHTS FOR ACADEMY AREA", 156.00),
    ("10/08/2026", "LALAMOVE", "TRANSPORT BANTING STANDS FROM USJ TO SPORTSINTEL (FOR LCW CUP EVENT)", 15.20),
    ("11/08/2026", "BAIFU (M) SDN BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP)", 112.30),
    ("13/08/2026", "BAIFU (M) SDN BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP)", 177.00),
    ("14/08/2026", "SSF CREATIVE LIFE CENTRE SDN BHD", "ITEMS FOR CAT DAY (DECORATIVE ITEMS)", 70.00),
    ("14/08/2026", "MR DIY SDN BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP)", 109.40),
    ("14/08/2026", "BFO MEGASTORE SDN BHD", "PANTRY SINK AND TAP", 249.00),
    ("14/08/2026", "LALAMOVE", "5 COMPUTER 5 MONITOR 5 KEYBOARDS & ACCESSORIES", 395.10),
    ("15/08/2026", "NOAI ENTERPRISE", "HDTV CABLE FOR RECEPTION PC", 39.00),
    ("16/08/2026", "TT PARCEL GLOBAL", "SHIPPING CHARGE FOR GROOMING ACCESSORIES PURCHASED FROM CHINA", 245.00),
    ("16/08/2026", "POPULAR BOOK CO (M) SDN BHD", "KEYBOARD FOR RECEPTION", 99.00),
    ("16/08/2026", "MR DIY SDN BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP)", 19.70),
    ("17/08/2026", "AREST CAFE DAMANSARA UTAMA SDN BHD", "LUNCH MEETING WITH MEOWBULOUS", 91.40),
    ("19/08/2026", "BOK MARKETING SDN BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP - HOTEL)", 168.20),
    ("20/08/2026", "BAIFU (M) SDN BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP)", 295.00),
    ("20/08/2026", "SSF CREATIVE LIFE CENTRE SDN BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP)", 153.50),
    ("20/08/2026", "SSF CREATIVE LIFE CENTRE SDN BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP)", 20.00),
    ("21/08/2026", "FORTUNE ELECTRICAL AND HARDWARE", "PENDANT LIGHT BULB REPLACEMENT (SIGNATURE ROOM)", 30.00),
    ("24/08/2026", "TRENDCELL SDN BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP)", 33.65),
    ("27/08/2026", "MICROSOFT", "WINDOWN11 PRO FOR OFFICE PC", 1299.00),
    ("28/08/2026", "AA BEST PRINTING SDN BHD", "BEACH FLAGS 5 SETS FOR LCW CUP EVENT", 1000.00),
    ("28/08/2026", "LALAMOVE", "TRANSPORTING BEACH FLAGS FROM PRINTING COMPANY TO STADIUM JUARA", 27.10),
    ("31/08/2026", "AEON CO. (M) BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP - HOTEL)", 400.75),
    ("31/08/2026", "AEON CO. (M) BHD", "ITEMS FOR CAT DAY (PRE OPENING SET UP)", 236.90),
    ("31/08/2026", "ALPRO ALLIANCE SDN BHD", "DAILY CLEANING ITEMS FOR OPERATION", 153.50),
    ("31/08/2026", "AEON DAISO BANDAR UTAMA", "DAILY CLEANING ITEMS FOR OPERATION", 59.00),
]
_CLAIM_SEP = [
    ("10/08/2026", "THE MOVERS ONLINE (M) SDN BHD", "MOVING AND RELOCATING", 455.80),
    ("23/08/2026", "JALI (MOVER SERVICE) 01169312001", "USJ TO CAT DAY", 300.00),
    ("25/08/2026", "GRABFOOD", "MOVING LUNCH FOR STAFFS", 49.00),
    ("25/08/2026", "LALAMOVE", "TRANSPSORTING ITEMS FROM USJ", 73.30),
    ("01/09/2026", "KITCHENCHANT SDN BHD", "NO PARKING SIGNAGE", 120.00),
    ("01/09/2026", "DILOOMA SDN BHD", "STICK VACUUM FOR BAORDING AREA", 104.00),
    ("02/09/2026", "POPULAR BOOK CO.(M) SDN BHD", "KEY TAGS TO ORGANISE SHOP KEYS", 9.40),
    ("02/09/2026", "MR D.I.Y. SDN BHD", "STORAGE ITEMS FOR SHOP", 46.50),
    ("02/09/2026", "GRABFOOD", "LUNCH MEETING", 44.74),
    ("04/09/2026", "TT PARCEL GLOBAL", "CHIPPING FEE FOR REPLACEMENT PARTS FOR THE CAGES", 421.00),
    ("07/09/2026", "ENG LEE HARDWARE (M) SDN BHD", "OUTDOOR DUSTBIN", 148.40),
    ("07/09/2026", "WUCHT BY CA IMPACT (M) SDN BHD", "CLEANER APRON AND CARRY CADDY", 59.30),
    ("08/09/2026", "GRABFOOD", "DRINKS FOR MARKETING (JOANNE & XINE) TEAM MEETING", 60.30),
    ("10/09/2026", "EADESS SDN BHD", "UTENSIL (CUP AND SAUCERS) FOR PANTRY AND GUESTS TOILET AMENITIES", 235.15),
    ("11/09/2026", "NITORI RETAIL (M) SDN BHD", "UTENSIL FOR GUESTS", 164.80),
    ("11/09/2026", "TRENDCELL SDN BHD", "CLEANING ITEMS", 55.10),
    ("12/09/2026", "KITTON (M) SDN BHD", "EYEDROP WITH SERUM FOR FIZ (DEVON REX)", 95.00),
    ("13/09/2026", "THE FOOD MERCHANT", "STANDBY STOCK FOR PANTRY (MINERAL WATER, CHILDREN DRINKS & SNACK)", 74.80),
    ("14/09/2026", "JAS DAILY OMAKASE", "BOOKED 20 LUNCH BOX FOR EVENT ON 18/9/26 (LUNCH GATHERING ACTUAL ARRIVED 25PAX)", 378.00),
    ("15/09/2026", "GRABFOOD", "MEETING WITH MARKETING TEAM", 85.70),
    ("17/09/2026", "MR D.I.Y. SDN BHD", "REPLANISH KIDS DRINKS & CLEANING ITEMS", 59.20),
    ("17/09/2026", "BEYOND ESSENTIAL SDN BHD", "BOARDING AREA CAT CLEANING ITEMS", 30.20),
    ("18/09/2026", "GRABFOOD", "ADD LUNCH ORDER FOR TEH PETER TOTH TALK LUNCH EVENT (ACTUAL ARRIVED 25PAX)", 94.18),
    ("16/09/2026", "ECO-SHOP MARKETING BERHAD", "GLASS CLEANER", 20.80),
    ("21/09/2026", "ASHVERTISING MARKETING SDN BHD", "DIFFUSER REFILL 100ML - HILTONE VILLA", 69.00),
    ("21/09/2026", "KITTON (M) SDN BHD", "MEDICATION FOR ORION (EYEINJURY)", 315.90),
    ("21/09/2026", "SSF CREATIVE LIFE CENTRE SDN BHD", "AMENITIES FOR SHOP", 106.00),
    ("21/09/2026", "EADESS SDN BHD", "SLIPPER FOR GUESTS TOILET (2 PAIRS)", 55.85),
    ("21/09/2026", "SENTAI KITCHENWARE SDN BHD", "UTENSIL FOR GUESTS", 134.40),
    ("23/09/2026", "REAL MEDIA SOLUTIONS", "CAT DAYNMENU SAMPLE PRODUCTION", 80.00),
    ("23/09/2026", "KITTON (M) SDN BHD", "KENZO & MEI MEI (CAST & OHE) FOR BOTH CATS", 346.00),
    ("23/09/2026", "ALL IT HYPERMARKET SDN BHD", "KEYBOARD & SOCKETS FOR CONCIERGE COUNTER", 178.00),
    ("24/09/2026", "BRUNNIE PASTRY", "PARTY BOX (FOR HI TEA EVENT ON 27/9- REA GATHERING)", 200.00),
    ("24/09/2026", "HYT FOOD INDUSTRIES SDN BHD", "MOONCAKE AS GIFT FOR CAT DAY NEIGHBOURS", 198.00),
    ("24/09/2026", "UNIQBE BAKERY SDN BHD", "PRIVATE HI TEA GATHERING ON 27/9 (35PAX- REA GYMNASTS TEAM)", 554.00),
    ("25/09/2026", "ECART SERVICES MALAYSIA SDN BHD", "TV STAND FOR EVENT USE", 135.52),
    ("27/09/2026", "MR D.I.Y SDN BHD", "UTENSIL FOR SHOP", 94.60),
]


def karen_claims_aug_sep_2026(db):
    key = "BACKFILL_CLAIMS_2608_2609"
    if db.get(M.Setting, key):
        return
    from .claims import guess_category, log
    karen = db.query(M.User).filter(M.User.username == "karen").first()
    for period, rows, expect in (("Aug 2026", _CLAIM_AUG, 5678.70), ("Sep 2026", _CLAIM_SEP, 5651.94)):
        total = round(sum(r[3] for r in rows), 2)
        if abs(total - expect) > 0.005:          # a typo here must not become a wrong claim
            print(f"  backfill: {period} claim adds to RM{total:,.2f}, list says RM{expect:,.2f} — NOT loaded")
            continue
        c = M.PettyClaim(claimant="Karen Sui", claimant_user_id=karen.id if karen else None,
                         period=period, created_by="Eugene (from Ms Lee's list)",
                         notes=f"Loaded from Ms Lee's {period} petty cash list (RM{expect:,.2f}). "
                               "Review the categories and the flagged lines, then submit.")
        db.add(c)
        db.flush()
        for i, (d, sup, desc, amt) in enumerate(rows):
            db.add(M.PettyClaimLine(claim_id=c.id, position=i,
                                    date=datetime.strptime(d, "%d/%m/%Y").date(),
                                    supplier=sup, description=desc,
                                    category=guess_category(sup, desc, amt), amount=amt))
        log(db, c, "system", "created", f"{len(rows)} lines · RM{total:,.2f} · from Ms Lee's Excel list")
        print(f"  backfill: draft claim {period} loaded — {len(rows)} lines, RM{total:,.2f}")
    db.add(M.Setting(key=key, value=datetime.utcnow().isoformat(timespec="seconds")))
    db.commit()


# Questions about specific lines on those two claims, put on the lines so the
# reviewer sees them where she decides. (0-based position, amount) -> question.
_Q_OTHER = ("Is this a cat day expense? It reads like the LCW Cup event / the academy. "
            "If another business should bear it, leave it out.")
_Q_EVENT = ("Is this a cat day expense? It is for an outside group's event. If another "
            "business should bear it, or it is recharged, leave it out.")
_Q_VET = ("Vet bill for a named cat. If it is a customer's cat, is it recharged to the owner "
          "or the shop's cost? (Kenzo & Mei Mei are Aster Yang's, INV-2609-002.)")
_CLAIM_QUERIES = {
    "Aug 2026": {2: (156.00, _Q_OTHER), 3: (15.20, _Q_OTHER), 9: (395.10, _Q_OTHER),
                 22: (1000.00, _Q_OTHER), 23: (27.10, _Q_OTHER)},
    "Sep 2026": {18: (378.00, _Q_EVENT), 22: (94.18, _Q_EVENT), 32: (200.00, _Q_EVENT),
                 34: (554.00, _Q_EVENT), 35: (135.52, _Q_EVENT),
                 16: (95.00, _Q_VET), 25: (315.90, _Q_VET), 30: (346.00, _Q_VET)},
}


def karen_claim_queries(db):
    key = "BACKFILL_CLAIM_QUERIES_2608_2609"
    if db.get(M.Setting, key):
        return
    n = 0
    for period, qs in _CLAIM_QUERIES.items():
        for c in db.query(M.PettyClaim).filter(M.PettyClaim.period == period,
                                               M.PettyClaim.created_by.like("Eugene (from Ms Lee%")).all():
            if c.status not in ("Draft", "Submitted", "Returned"):
                continue                      # already decided: nothing to ask
            lines = list(c.lines)
            for pos, (amt, q) in qs.items():
                # Only if the line is still where it was and still that amount --
                # she may have edited the draft since it was loaded.
                if pos < len(lines) and abs(lines[pos].amount - amt) < 0.005 and not lines[pos].query:
                    lines[pos].query = q
                    n += 1
    db.add(M.Setting(key=key, value=datetime.utcnow().isoformat(timespec="seconds")))
    db.commit()
    print(f"  backfill: {n} review questions added to the Aug/Sep claim lines")


def run_all(db):
    ar_lines_sept_2026(db)
    attendance_roster_from_payroll(db)
    karen_claims_aug_sep_2026(db)
    karen_claim_queries(db)
