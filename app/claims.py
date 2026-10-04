"""Petty cash claims: the rules.

A claim is many small receipts one person paid for, reimbursed together.
Posting a claim turns each line into a Payment in its own expense category,
owed to the claimant -- so everything downstream already works: one voucher
for the lot, the payment listing, the approver link, the P&L by category.

Nothing is posted until a reviewer approves the claim, and the reviewer is
never the claimant.
"""
from datetime import datetime, timedelta

from sqlalchemy import func

from . import models as M

# Suggestions only -- they pre-fill the category and the reviewer has the last
# word. A few shops are unmistakable by name; otherwise the PURPOSE decides,
# never the shop's name ("ECO-SHOP MARKETING" selling glass cleaner is not
# marketing). First match wins, so the specific comes before the general.
SUPPLIER_RULES = [
    ("Vet", ["kitton", "vet ", "veterinar", "animal clinic"]),
    ("Software", ["microsoft", "adobe", "canva"]),
    ("Transport", ["lalamove", "tt parcel", "mover", "j&t", "pos laju", "ninja van"]),
]
CATEGORY_KEYWORDS = [
    ("Vet", ["vet", "clinic", "medication", "medicine", "eyedrop", "eye drop", "serum",
             "vaccin", "deworm", "deflea", "surgery"]),
    ("Software", ["windows", "window11", "software", "licence", "license", "subscription"]),
    ("Equipment", ["computer", "monitor", "keyboard", "printer", "tv stand", "television", "vacuum",
                   "sink", "track light", "cctv", "router", "dustbin", "hdtv", "socket"]),
    ("Marketing", ["event", "gathering", "hi tea", "hi-tea", "marketing", "flag", "banner", "bunting",
                   "banting", "menu", "gift", "mooncake", "party box"]),
    ("Staff Welfare", ["lunch", "dinner", "meal", "drinks", "snack", "mineral water"]),
    ("Transport", ["moving", "relocat", "transport", "shipping", "chipping", "courier", "delivery",
                   "toll", "parking fee", "petrol"]),
    ("Grooming Supplies", ["grooming", "shampoo", "conditioner", "clipper"]),
    ("Cat Supplies", ["litter", "cat food", "kibble", "cat cleaning", "cage", "cat toy", "scratch"]),
    ("Maintenance", ["repair", "replacement", "bulb", "cleaning", "cleaner", "detergent", "apron",
                     "plumb"]),
    ("Admin", ["stationery", "key tag", "storage", "utensil", "amenit", "slipper", "diffuser",
               "tape", "signage", "set up", "setup", "decorative", "pantry"]),
]
# Below this, something that sounds like equipment is suggested as an Admin
# expense instead: "Equipment" posts to the fixed-asset account, and a RM39
# cable is not an asset. The reviewer can still choose Equipment.
EQUIPMENT_MIN = 300.0


def _has(text: str, word: str) -> bool:
    """`word` appears starting at a word boundary ("sink", not "pursinks")."""
    i = text.find(word)
    while i >= 0:
        if i == 0 or not text[i - 1].isalpha():
            return True
        i = text.find(word, i + 1)
    return False


def guess_category(supplier: str, description: str, amount: float = 0.0) -> str:
    sup, desc = (supplier or "").lower() + " ", (description or "").lower()
    cat = "Misc"
    for c, words in SUPPLIER_RULES:
        if any(_has(sup, w) for w in words):
            cat = c
            break
    else:
        for c, words in CATEGORY_KEYWORDS:
            if any(_has(desc, w) for w in words):
                cat = c
                break
    if cat == "Equipment" and amount < EQUIPMENT_MIN:
        cat = "Admin"
    return cat


def log(db, claim, who: str, action: str, detail: str = ""):
    db.add(M.PettyClaimEvent(claim_id=claim.id, who=who, action=action, detail=detail[:2000]))


def _key(name: str) -> str:
    """First two words of a business name, for a loose 'same shop?' match."""
    words = [w for w in "".join(c if c.isalnum() else " " for c in (name or "").lower()).split()
             if w not in ("sdn", "bhd", "m", "the", "co", "enterprise")]
    return " ".join(words[:2])


def warnings(db, claim) -> dict:
    """Things the reviewer should look at before approving. Returns
    {'lines': {line_id: [text]}, 'claim': [text]}. Nothing here blocks --
    the reviewer decides -- but nothing is hidden either."""
    out = {"lines": {}, "claim": []}
    own_ids = {p.id for p in claim.payments}
    others = [p for p in db.query(M.Payment).filter(M.Payment.status != "Void").all()
              if p.id not in own_ids]

    for l in claim.lines:
        notes = []
        k = _key(l.supplier)
        # 1. The same shop already has a payment for the same amount: was this
        #    paid by the company directly, as well as claimed?
        if k and len(k) >= 4:
            for p in others:
                if p.claim_id:
                    continue
                if abs(p.amount - l.amount) < 0.005 and abs((p.date - l.date).days) <= 45 \
                        and (k in _key(p.supplier) or _key(p.supplier) in k) and _key(p.supplier):
                    pv = f", {p.voucher.pv_no}" if p.voucher else ""
                    notes.append(f"Already in the system as {p.pay_no}{pv} — {p.supplier}, "
                                 f"RM{p.amount:,.2f} on {p.date:%d/%m/%Y}. Paid by the company "
                                 f"directly? Then exclude this line, or it is paid twice.")
        # 2. The same receipt on another claim.
        twins = db.query(M.PettyClaimLine).filter(
            M.PettyClaimLine.id != l.id, M.PettyClaimLine.date == l.date,
            M.PettyClaimLine.amount == l.amount, M.PettyClaimLine.excluded == False,  # noqa: E712
            func.lower(M.PettyClaimLine.supplier) == (l.supplier or "").lower()).all()
        for t in twins:
            where = (t.claim.claim_no or f"draft #{t.claim_id}") if t.claim_id != claim.id else "this claim"
            notes.append(f"Same date, shop and amount as a line on {where} — claimed twice?")
        if notes:
            out["lines"][l.id] = notes

    # 3. Is the claimant already being paid some other way?
    ck = _key(claim.claimant)
    if ck:
        cutoff = (claim.created_at or datetime.utcnow()).date() - timedelta(days=120)
        for p in others:
            if not p.claim_id and p.date >= cutoff and ck.split()[0] in (p.supplier or "").lower():
                pv = f", {p.voucher.pv_no}" if p.voucher else ""
                out["claim"].append(f"{p.pay_no}{pv}: {p.supplier}, RM{p.amount:,.2f}, "
                                    f"{p.date:%d/%m/%Y} — “{(p.description or '')[:60]}”")
    return out


def ensure_claimant_supplier(db, name: str):
    """The voucher prints bank details from the supplier directory, so the
    claimant needs a record there."""
    s = db.query(M.Supplier).filter(func.lower(M.Supplier.name) == name.strip().lower()).first()
    if not s:
        db.add(M.Supplier(name=name.strip(), sup_type="Staff",
                          notes="Created from a petty cash claim — add bank details so the "
                                "voucher can print where to pay."))
    elif not s.active:
        s.active = True


def post(db, claim, user, next_pay_no, month_str) -> int:
    """Approve: every included line becomes a Payment owed to the claimant."""
    ensure_claimant_supplier(db, claim.claimant)
    n = 0
    for i, l in enumerate(claim.lines, 1):
        if l.excluded or l.amount <= 0 or l.payment_id:
            continue
        p = M.Payment(pay_no=next_pay_no(), date=l.date, supplier=claim.claimant,
                      invoice_no=f"{claim.claim_no}/{i:02d}",
                      description=f"{l.supplier} — {l.description}" if l.supplier else l.description,
                      category=l.category, grp=M.group_for(l.category), amount=l.amount,
                      month=month_str(l.date), status="Categorized",
                      notes=f"Petty cash claim {claim.claim_no} ({claim.claimant})",
                      claim_id=claim.id)
        db.add(p)
        db.flush()
        l.payment_id = p.id
        n += 1
    claim.status = "Posted"
    claim.reviewed_by, claim.reviewed_at = user.display_name, datetime.utcnow()
    return n


def can_unpost(claim) -> str:
    """'' if the posting can be undone, else why not."""
    for p in claim.payments:
        if p.voucher_id or p.status not in ("Unsorted", "Categorized"):
            pv = p.voucher.pv_no if p.voucher else p.status
            return (f"{p.pay_no} is already on {pv}. Delete that voucher first "
                    f"(Vouchers page), then this claim can be reopened.")
    return ""
