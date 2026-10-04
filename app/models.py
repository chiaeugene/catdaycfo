from datetime import datetime, date
from sqlalchemy import String, Integer, Float, Date, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

# ── Constants ────────────────────────────────────────────────────────────────
# "viewer" is read-only system-wide — enforced centrally in app/audit.py's
# AccessControlMiddleware, not by individual routes (many older routes never
# had their own role check, so a per-route approach would miss some).
ROLES = ["admin", "manager", "staff", "viewer", "requester", "storekeeper"]

# What the shop sells, so an invoice is picked rather than typed. Prices are
# the defaults the form fills in -- every line stays editable, because promos
# and founding-customer rates are real and frequent. `per` is the unit the
# quantity counts ("night" makes the form offer check-in/check-out dates and
# count the nights itself; Karen's hand counts were off three times in Sept).
SERVICE_CATALOGUE = [
    # (label, stream, unit price, per)
    ("Premium Grooming", "Grooming", 138.0, "cat"),
    ("Premium Grooming (RM99 promo)", "Grooming", 99.0, "cat"),
    ("The Haven boarding", "Boarding", 88.0, "night"),
    ("The Big Nook boarding", "Boarding", 0.0, "night"),
    ("Signature Cat Day Plan", "Membership", 1899.0, "plan"),
    ("Comfort Recovery Plan", "Membership", 2099.0, "plan"),
    ("Founding Family Circle privilege rate", "Boarding", 0.0, ""),
]

# ── Purchase requests ──────────────────────────────────────────────────────
# Approval before spending. Deliberately outside the ledger: a request posts
# nothing, it only records that someone with the authority said yes.
PR_STATUS = ["Draft", "Submitted", "Returned", "Approved", "Rejected", "Cancelled", "Fulfilled"]
PR_OPEN = ("Draft", "Submitted", "Returned")            # still being worked on
PR_URGENCY = ["Normal", "Urgent"]
PR_PURPOSES = ["Restock", "Replace broken", "New item", "Repair", "Event / marketing", "Other"]
PR_TERMS = ["Credit", "Cash on delivery", "Cash before delivery"]
UOMS = ["pcs", "box", "bottle", "pack", "carton", "bag", "kg", "g", "L", "ml",
        "set", "pair", "roll", "service", "month", "hour"]
# Regular commitments that never go through a purchase request — flagging
# every rent and salary payment as "no approval" would train people to ignore
# the flag on the ones that matter.
APPROVAL_EXEMPT = {"Rental", "Utilities", "Salary", "Insurance", "Software", "Staff Claim"}
PR_APPROVAL_DAYS = 30                                  # an approval goes stale after this

CATEGORIES = [
    "Renovation", "Equipment", "Cat Supplies", "Grooming Supplies", "Utilities",
    "Rental", "Salary", "Staff Claim", "Marketing", "Insurance", "Software", "Transport",
    "Admin", "Maintenance", "Staff Welfare", "Vet", "Misc",
]
GROUPS = ["CAPEX", "OPEX", "COGS", "Payroll", "Petty Cash"]

# Which P&L group a category belongs to (cat-hotel logic).
# Consumable goods used to deliver boarding/grooming = COGS; assets = CAPEX;
# services/overheads = OPEX; salary = Payroll.
CATEGORY_GROUP = {
    "Renovation": "CAPEX", "Equipment": "CAPEX",
    "Cat Supplies": "COGS", "Grooming Supplies": "COGS", "Vet": "COGS",
    "Salary": "Payroll",
}


def group_for(category: str, section: str = "") -> str:
    if category in CATEGORY_GROUP:
        return CATEGORY_GROUP[category]
    return "CAPEX" if section == "Purchase" else "OPEX"
DOC_TYPES = ["Invoice", "Proforma Invoice", "Receipt", "Quotation", "Statement",
             "Bank-in Slip", "Payslip", "Other"]
# Document types that are a REQUEST for payment, not a bill. They create no
# liability: the payable only exists once the real tax invoice arrives. Posting
# one as a purchase invents a creditor and then double-counts when the final
# invoice follows.
PROVISIONAL_DOC_TYPES = ("Proforma Invoice", "Quotation")
# Section = where a verified submission is routed
DOC_SECTIONS = ["Purchase", "Expense", "Staff Claim", "Petty Cash", "Sales Report",
                "Customer Receipt", "Boarding Log", "Bank-in Slip", "Payroll", "Filing Only"]
# Intake type = what kind of thing the bot received
INTAKE_TYPES = ["Document", "Sales Report", "Petty Cash", "Staff Claim", "Boarding Log"]
DOC_STATUS = ["Pending", "Verified", "Rejected"]
PAY_STATUS = ["Unsorted", "Categorized", "On Voucher", "Paid"]
PV_STATUS = ["Draft", "Approved", "Paid", "Void"]
PL_STATUS = ["Draft", "Submitted", "Processed"]
STREAMS = ["Boarding", "Grooming", "Cat Sales", "Membership", "Retail", "Other"]
PAY_METHODS = ["Cash", "Bank Transfer", "Card", "TNG", "Cheque"]

# Malaysian SST — service tax 6%/8% on services; sales tax 10% on goods.
TAX_TYPES = {"None": 0.0, "SST 6%": 0.06, "SST 8%": 0.08, "Sales Tax 10%": 0.10}

# Malaysian banks + their bulk-payment / IBG file layouts. `cols` is the column
# order the bank's enterprise portal expects. VALIDATE against the bank's own
# downloaded template before first live upload — banks revise these.
MY_BANK_FORMATS = {
    "Maybank (M2E/Maybank2u Biz)": {
        "code": "MBB", "cols": ["Payment Type", "Beneficiary Name", "Beneficiary Account",
                                  "Bank Code", "Amount", "Reference", "Email"]},
    "CIMB (BizChannel)": {
        "code": "CIMB", "cols": ["Beneficiary Name", "Beneficiary Account", "Bank",
                                   "Amount", "Payment Reference", "Beneficiary Reference"]},
    "Public Bank (PBe Biz)": {
        "code": "PBB", "cols": ["Account No", "Beneficiary Name", "Bank Code",
                                 "Amount", "Reference", "Payment Description"]},
    "RHB (Reflex)": {
        "code": "RHB", "cols": ["Beneficiary Name", "Account No", "Bank Code",
                                 "Amount", "Payment Ref", "Recipient Ref"]},
    "Hong Leong (ConnectFirst)": {
        "code": "HLB", "cols": ["Beneficiary Name", "Beneficiary Account", "Bank Code",
                                 "Amount (RM)", "Reference", "Description"]},
    "AmBank (AmAccess Biz)": {
        "code": "AMB", "cols": ["Beneficiary Name", "Account Number", "Bank",
                                 "Amount", "Reference No", "Remarks"]},
    "Generic IBG / DuitNow": {
        "code": "GEN", "cols": ["Beneficiary Name", "Account Number", "Bank Name",
                                 "Amount", "Reference"]},
}
# Bank codes (BIC/clearing) for the beneficiary bank column
MY_BANK_CODES = {
    "Maybank": "MBBEMYKL", "CIMB Bank": "CIBBMYKL", "Public Bank": "PBBEMYKL",
    "RHB Bank": "RHBBMYKL", "Hong Leong Bank": "HLBBMYKL", "AmBank": "ARBKMYKL",
    "Bank Islam": "BIMBMYKL", "OCBC Bank": "OCBCMYKL", "UOB Bank": "UOVBMYKL",
    "Alliance Bank": "MFBBMYKL",
}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default="staff")
    telegram_id: Mapped[str] = mapped_column(String(30), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Separate from role on purpose: Jasmine is a viewer who changes nothing in
    # the books, and approving a purchase request is not a change to the books.
    can_approve: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    """Every mutating action, who did it, and whether it was actually allowed
    through. Written by AccessControlMiddleware — not by individual routes —
    so nothing can bypass it by forgetting to log."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    user_name: Mapped[str] = mapped_column(String(100), default="")   # captured at the time — survives renames
    method: Mapped[str] = mapped_column(String(10), default="")
    path: Mapped[str] = mapped_column(String(300), default="")
    query: Mapped[str] = mapped_column(String(300), default="")
    action: Mapped[str] = mapped_column(String(200), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_no: Mapped[str] = mapped_column(String(20), unique=True)   # DOC-0001
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sender: Mapped[str] = mapped_column(String(100), default="")
    section: Mapped[str] = mapped_column(String(30), default="Expense")   # routing target
    doc_type: Mapped[str] = mapped_column(String(30), default="Other")
    supplier: Mapped[str] = mapped_column(String(150), default="")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    month: Mapped[str] = mapped_column(String(20), default="")     # "Jul 2026"
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), default="")
    invoice_no: Mapped[str] = mapped_column(String(60), default="")
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)   # invoice/receipt date read off the document
    # Final tax invoice → the proforma/quotation it replaces, so the pair stays
    # together and the same order can't be paid twice.
    related_doc_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id"), nullable=True)
    intake_type: Mapped[str] = mapped_column(String(30), default="Document")
    payload_json: Mapped[str] = mapped_column(Text, default="")     # structured data for reports
    raw_text: Mapped[str] = mapped_column(Text, default="")         # original message text
    file_path: Mapped[str] = mapped_column(String(300), default="") # relative to uploads/ (blank for text)
    mime: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(30), default="Pending")
    ai_classified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[str] = mapped_column(String(100), default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    pay_no: Mapped[str] = mapped_column(String(20), unique=True)   # PAY-0001
    date: Mapped[date] = mapped_column(Date, default=date.today)
    supplier: Mapped[str] = mapped_column(String(150), default="")
    invoice_no: Mapped[str] = mapped_column(String(60), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), default="")
    grp: Mapped[str] = mapped_column(String(30), default="")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    tax_type: Mapped[str] = mapped_column(String(20), default="None")
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0)
    month: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(30), default="Unsorted")
    voucher_id: Mapped[int | None] = mapped_column(ForeignKey("vouchers.id"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # The approval this spend was made under, if any. Nullable: rent is never
    # requested, and a payment linked to nothing simply carries a flag.
    request_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_requests.id"), nullable=True)
    # Set when this payment is one line of a staff petty cash claim. The
    # "supplier" is then the claimant being reimbursed; the shop is in the
    # description. One claim becomes one voucher.
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("petty_claims.id"), nullable=True)
    documents = relationship("Document", backref="payment", foreign_keys="Document.payment_id")


class Voucher(Base):
    __tablename__ = "vouchers"
    id: Mapped[int] = mapped_column(primary_key=True)
    pv_no: Mapped[str] = mapped_column(String(20), unique=True)    # PV-0001
    date: Mapped[date] = mapped_column(Date, default=date.today)
    payee: Mapped[str] = mapped_column(String(150), default="")
    total: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="Draft")
    pdf_path: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    approved_by: Mapped[str] = mapped_column(String(100), default="")
    listing_id: Mapped[int | None] = mapped_column(ForeignKey("listings.id"), nullable=True)
    payments = relationship("Payment", backref="voucher", foreign_keys="Payment.voucher_id")


class Listing(Base):
    __tablename__ = "listings"
    id: Mapped[int] = mapped_column(primary_key=True)
    pl_no: Mapped[str] = mapped_column(String(20), unique=True)    # PL-0001
    date: Mapped[date] = mapped_column(Date, default=date.today)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="Draft")
    pdf_path: Mapped[str] = mapped_column(String(300), default="")
    prepared_by: Mapped[str] = mapped_column(String(100), default="")
    vouchers = relationship("Voucher", backref="listing", foreign_keys="Voucher.listing_id")


class ListingShare(Base):
    """A read-only public link to one payment listing, for the authoriser.

    The authoriser is usually a director who has no login and no reason to want
    one — they need to see the vouchers, the supplier bank details and the
    original invoices, once, on their phone. A long random token is the whole
    credential, so it is generated with secrets.token_urlsafe and can be revoked
    or rotated at any time; every view is stamped so the preparer can tell
    whether it was actually opened.
    """
    __tablename__ = "listing_shares"
    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    views: Mapped[int] = mapped_column(Integer, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    listing = relationship("Listing", backref="shares")

    @property
    def live(self) -> bool:
        if self.revoked:
            return False
        return not (self.expires_at and self.expires_at < datetime.utcnow())

    @property
    def state(self) -> str:
        if self.revoked:
            return "Revoked"
        if self.expires_at and self.expires_at < datetime.utcnow():
            return "Expired"
        return "Live"


class PurchaseRequest(Base):
    """'May I buy this?' -- asked before the money moves.

    The operator fills the form; anyone with can_approve says yes or no. Nothing
    here touches the ledger. The value is the trail: what was asked, what was
    approved, by whom, and whether the invoice that later arrives matches it.
    """
    __tablename__ = "purchase_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    pr_no: Mapped[str] = mapped_column(String(20), default="")    # blank until submitted
    status: Mapped[str] = mapped_column(String(20), default="Draft")
    requester_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    requester: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    needed_by: Mapped[date | None] = mapped_column(Date, nullable=True)
    urgency: Mapped[str] = mapped_column(String(10), default="Normal")
    supplier: Mapped[str] = mapped_column(String(150), default="")
    supplier_new: Mapped[bool] = mapped_column(Boolean, default=False)   # typed in, not on file
    purpose: Mapped[str] = mapped_column(String(40), default="Restock")
    reason: Mapped[str] = mapped_column(Text, default="")
    terms: Mapped[str] = mapped_column(String(30), default="Credit")
    total: Mapped[float] = mapped_column(Float, default=0.0)
    photo_path: Mapped[str] = mapped_column(String(300), default="")
    # Decision
    decided_by: Mapped[str] = mapped_column(String(100), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    pdf_path: Mapped[str] = mapped_column(String(300), default="")
    # What exactly was approved, frozen at the moment of approval. Any later edit
    # cancels the approval, so this and the live lines only differ in history.
    approved_json: Mapped[str] = mapped_column(Text, default="")
    lines = relationship("PurchaseRequestLine", backref="request",
                         cascade="all, delete-orphan", order_by="PurchaseRequestLine.id")
    events = relationship("PurchaseRequestEvent", backref="request",
                          cascade="all, delete-orphan", order_by="PurchaseRequestEvent.id")
    payments = relationship("Payment", backref="request", foreign_keys="Payment.request_id")

    @property
    def is_open(self) -> bool:
        return self.status in PR_OPEN

    @property
    def editable(self) -> bool:
        return self.status in ("Draft", "Returned")

    @property
    def stale(self) -> bool:
        return (self.status == "Approved" and self.expires_at is not None
                and self.expires_at < date.today())

    @property
    def invoiced(self) -> float:
        return sum(p.amount for p in self.payments if p.status != "Void")


class PurchaseRequestLine(Base):
    __tablename__ = "purchase_request_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("purchase_requests.id"))
    item: Mapped[str] = mapped_column(String(150), default="")
    category: Mapped[str] = mapped_column(String(50), default="Misc")
    qty: Mapped[float] = mapped_column(Float, default=1.0)
    uom: Mapped[str] = mapped_column(String(20), default="pcs")
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    stock_item_id: Mapped[int | None] = mapped_column(ForeignKey("stock_items.id"), nullable=True)

    @property
    def amount(self) -> float:
        return round(self.qty * self.unit_price, 2)


class PurchaseRequestEvent(Base):
    """Append-only. Nothing here is ever edited or deleted -- it is the trail."""
    __tablename__ = "purchase_request_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("purchase_requests.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    who: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(30), default="")     # created/submitted/approved/...
    detail: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[str] = mapped_column(String(10), default="web") # web / telegram


# ─────────────────────────── Petty cash claims ───────────────────────────
# A staff member pays for small things herself all month and claims the total
# back. The claim is the unit: many receipts, each in its own expense category,
# reimbursed by ONE voucher. Before this existed the list lived in Excel,
# because the Petty Cash page takes one line at a time with no voucher, and
# the "Staff Claim" route files every receipt under a single account.
CLAIM_STATUS = ["Draft", "Submitted", "Returned", "Posted", "Paid"]


class PettyClaim(Base):
    __tablename__ = "petty_claims"
    id: Mapped[int] = mapped_column(primary_key=True)
    claim_no: Mapped[str] = mapped_column(String(20), default="")      # PC-2609-001, on submit
    claimant: Mapped[str] = mapped_column(String(100), default="")     # who is reimbursed
    claimant_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    period: Mapped[str] = mapped_column(String(20), default="")        # "Sep 2026"
    status: Mapped[str] = mapped_column(String(12), default="Draft")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[str] = mapped_column(String(100), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    return_note: Mapped[str] = mapped_column(Text, default="")
    pdf_path: Mapped[str] = mapped_column(String(300), default="")
    lines = relationship("PettyClaimLine", backref="claim", cascade="all, delete-orphan",
                         order_by="PettyClaimLine.position, PettyClaimLine.id")
    files = relationship("PettyClaimFile", backref="claim", cascade="all, delete-orphan")
    events = relationship("PettyClaimEvent", backref="claim", cascade="all, delete-orphan",
                          order_by="PettyClaimEvent.id")
    payments = relationship("Payment", backref="claim", foreign_keys="Payment.claim_id")

    @property
    def total_claimed(self) -> float:
        return round(sum(l.amount for l in self.lines), 2)

    @property
    def total(self) -> float:
        """What will actually be reimbursed: the claimed lines less any the
        reviewer excluded."""
        return round(sum(l.amount for l in self.lines if not l.excluded), 2)

    @property
    def editable(self) -> bool:
        return self.status in ("Draft", "Returned")

    @property
    def paid(self) -> bool:
        live = [p for p in self.payments if p.status != "Void"]
        return bool(live) and all(p.status == "Paid" for p in live)


class PettyClaimLine(Base):
    __tablename__ = "petty_claim_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("petty_claims.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    date: Mapped[date] = mapped_column(Date, default=date.today)
    supplier: Mapped[str] = mapped_column(String(150), default="")     # the shop paid
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), default="Misc")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    # The reviewer can leave a line out (already paid another way, not a
    # company expense) without deleting what was claimed.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    exclude_reason: Mapped[str] = mapped_column(Text, default="")
    # A question for the reviewer about this line, shown beside it until the
    # claim is approved ("is this a cat day expense?").
    query: Mapped[str] = mapped_column(Text, default="")
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)


class PettyClaimFile(Base):
    __tablename__ = "petty_claim_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("petty_claims.id"))
    path: Mapped[str] = mapped_column(String(300))
    name: Mapped[str] = mapped_column(String(200), default="")
    uploaded_by: Mapped[str] = mapped_column(String(100), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PettyClaimEvent(Base):
    __tablename__ = "petty_claim_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("petty_claims.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    who: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(30), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


# ─────────────────────────── Attendance ───────────────────────────
# Kept apart from payroll's Staff on purpose: part-timers clock in but aren't
# on payroll, and payroll picks up every active Staff row. Karen manages this
# roster; payroll is never touched by it. `staff_id` links a person to their
# payroll record when there is one, for a future payroll feed.
class AttendancePerson(Base):
    __tablename__ = "attendance_people"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    position: Mapped[str] = mapped_column(String(100), default="")
    employment: Mapped[str] = mapped_column(String(20), default="Full-time")
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    pin_hash: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    devices = relationship("AttendanceDevice", backref="person", cascade="all, delete-orphan")


class AttendanceKiosk(Base):
    """The device at the counter that shows the live QR. Authorised once by a
    logged-in user; after that it's recognised by a long-lived cookie, so the
    screen works logged out. Its location, captured at set-up, is the shop."""
    __tablename__ = "attendance_kiosks"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    secret: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(60), default="Counter")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    authorised_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class AttendanceDevice(Base):
    """A staff member's phone. The first scan from a phone links it pending
    Karen's confirmation -- otherwise anyone could pick a colleague's name."""
    __tablename__ = "attendance_devices"
    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("attendance_people.id"))
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending/confirmed/rejected
    set_pin: Mapped[bool] = mapped_column(Boolean, default=False)       # this phone chose the PIN
    user_agent: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    decided_by: Mapped[str] = mapped_column(String(100), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AttendanceLog(Base):
    """One clock-in or clock-out. Time is the server's, never the phone's.
    Never edited: a wrong scan is voided (status 'rejected', with a reason)
    and a missing one is added as 'manual' by an admin, with a reason."""
    __tablename__ = "attendance_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("attendance_people.id"))
    kind: Mapped[str] = mapped_column(String(4))                       # in / out
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("attendance_devices.id"), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    selfie_path: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(10), default="ok")      # ok/pending/rejected/manual
    flags: Mapped[str] = mapped_column(String(200), default="")        # comma list
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")   # set for manual entries
    person = relationship("AttendancePerson")
    device = relationship("AttendanceDevice")


class AttendanceRemark(Base):
    """Karen's notes on a person's day: overtime (with hours) or a reason
    (late, left early, absent). She annotates; she doesn't change the times."""
    __tablename__ = "attendance_remarks"
    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("attendance_people.id"))
    day: Mapped[date] = mapped_column(Date, index=True)
    kind: Mapped[str] = mapped_column(String(10), default="Reason")    # OT / Reason
    hours: Mapped[float] = mapped_column(Float, default=0.0)
    text: Mapped[str] = mapped_column(Text, default="")
    by: Mapped[str] = mapped_column(String(100), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    person = relationship("AttendancePerson")


class PettyCashAccount(Base):
    """A company may run several petty-cash tins/floats (e.g. Front Desk, Grooming)."""
    __tablename__ = "petty_cash_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    float_target: Mapped[float] = mapped_column(Float, default=5000.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class PettyCashEntry(Base):
    __tablename__ = "petty_cash"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("petty_cash_accounts.id"), nullable=True)
    date: Mapped[date] = mapped_column(Date, default=date.today)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), default="")
    amount_out: Mapped[float] = mapped_column(Float, default=0.0)
    amount_in: Mapped[float] = mapped_column(Float, default=0.0)
    month: Mapped[str] = mapped_column(String(20), default="")
    recorded_by: Mapped[str] = mapped_column(String(100), default="")
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    document = relationship("Document")
    account = relationship("PettyCashAccount")


class SalesEntry(Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, default=date.today)
    stream: Mapped[str] = mapped_column(String(30), default="Boarding")
    description: Mapped[str] = mapped_column(Text, default="")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    tax_type: Mapped[str] = mapped_column(String(20), default="None")
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[str] = mapped_column(String(30), default="Cash")
    month: Mapped[str] = mapped_column(String(20), default="")
    recorded_by: Mapped[str] = mapped_column(String(100), default="")
    # Number of services performed (grooming sessions, boarding nights…).
    # Feeds the stock-usage engine: sessions × recipe = consumables used.
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    # Which daily report this row came from. Lets the ledger post one balanced
    # entry per day — revenue split by service, money split by payment method —
    # instead of assuming every row landed in the bank.
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=True)


class StockItem(Base):
    """A consumable or tool whose usage we track against services — shampoo,
    combs, gloves. On-hand is DERIVED, never stored: purchases/adjustments
    (StockMovement) minus computed usage (sales sessions × ServiceRecipe).
    Rebuildable and always consistent, same philosophy as the ledger."""
    __tablename__ = "stock_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40), default="Grooming Supplies")
    unit: Mapped[str] = mapped_column(String(40), default="pcs")   # bottle (5L) / pcs / pack
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)   # RM per unit
    reorder_level: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    movements = relationship("StockMovement", backref="item", cascade="all, delete-orphan")
    recipes = relationship("ServiceRecipe", backref="item", cascade="all, delete-orphan")


class StockMovement(Base):
    """Stock in/out that ISN'T service usage: purchases (link the PAY ref so
    stock ties back to the invoice trail), stocktake adjustments, wastage."""
    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id"))
    date: Mapped[date] = mapped_column(Date, default=date.today)
    qty: Mapped[float] = mapped_column(Float, default=0.0)         # + in, − out
    kind: Mapped[str] = mapped_column(String(20), default="Purchase")  # Purchase / Adjustment
    ref: Mapped[str] = mapped_column(String(60), default="")       # e.g. PAY-0012
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)   # RM/unit on this purchase
    notes: Mapped[str] = mapped_column(String(200), default="")
    recorded_by: Mapped[str] = mapped_column(String(100), default="")


class ServiceRecipe(Base):
    """How much of an item one service consumes — Eugene's '1 grooming uses
    5% of a shampoo bottle'. qty_per_service is in item units, so 5% of a
    bottle = 0.05. Stream matches the sales stream (Grooming, Boarding…)."""
    __tablename__ = "service_recipes"
    id: Mapped[int] = mapped_column(primary_key=True)
    stream: Mapped[str] = mapped_column(String(30), default="Grooming")
    item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id"))
    qty_per_service: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class BoardingLog(Base):
    __tablename__ = "boarding_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, default=date.today)
    checked_in: Mapped[int] = mapped_column(Integer, default=0)
    checked_out: Mapped[int] = mapped_column(Integer, default=0)
    occupancy: Mapped[int] = mapped_column(Integer, default=0)   # cats in-house at end of day
    notes: Mapped[str] = mapped_column(Text, default="")
    recorded_by: Mapped[str] = mapped_column(String(100), default="")


class Staff(Base):
    __tablename__ = "staff"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    position: Mapped[str] = mapped_column(String(100), default="")
    nric: Mapped[str] = mapped_column(String(30), default="")
    bank_account: Mapped[str] = mapped_column(String(50), default="")
    base_salary: Mapped[float] = mapped_column(Float, default=0.0)
    allowance: Mapped[float] = mapped_column(Float, default=0.0)
    epf_employer: Mapped[float] = mapped_column(Float, default=0.0)
    epf_employee: Mapped[float] = mapped_column(Float, default=0.0)
    socso_employer: Mapped[float] = mapped_column(Float, default=0.0)
    socso_employee: Mapped[float] = mapped_column(Float, default=0.0)
    eis_employer: Mapped[float] = mapped_column(Float, default=0.0)
    eis_employee: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def gross(self):
        return self.base_salary + self.allowance

    @property
    def net_pay(self):
        return self.gross - self.epf_employee - self.socso_employee - self.eis_employee

    @property
    def employer_cost(self):
        return self.gross + self.epf_employer + self.socso_employer + self.eis_employer


class PayrollRun(Base):
    __tablename__ = "payroll_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(20))                 # "Jul 2026"
    run_date: Mapped[date] = mapped_column(Date, default=date.today)
    total_net: Mapped[float] = mapped_column(Float, default=0.0)     # take-home total
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)    # employer cost total
    status: Mapped[str] = mapped_column(String(20), default="Draft")  # Draft → Confirmed
    items = relationship("PayrollItem", backref="run", cascade="all, delete-orphan")


class PayrollItem(Base):
    __tablename__ = "payroll_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"))
    staff_name: Mapped[str] = mapped_column(String(100))
    position: Mapped[str] = mapped_column(String(100), default="")
    base: Mapped[float] = mapped_column(Float, default=0.0)
    allowance: Mapped[float] = mapped_column(Float, default=0.0)
    overtime: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    bonus: Mapped[float] = mapped_column(Float, default=0.0)
    unpaid_leave_days: Mapped[float] = mapped_column(Float, default=0.0)
    leave_deduction: Mapped[float] = mapped_column(Float, default=0.0)   # RM docked for unpaid leave
    epf_er: Mapped[float] = mapped_column(Float, default=0.0)
    epf_ee: Mapped[float] = mapped_column(Float, default=0.0)
    socso_er: Mapped[float] = mapped_column(Float, default=0.0)
    socso_ee: Mapped[float] = mapped_column(Float, default=0.0)
    eis_er: Mapped[float] = mapped_column(Float, default=0.0)
    eis_ee: Mapped[float] = mapped_column(Float, default=0.0)
    pcb: Mapped[float] = mapped_column(Float, default=0.0)          # monthly tax deduction (MTD)
    deductions: Mapped[float] = mapped_column(Float, default=0.0)   # other deductions
    remarks: Mapped[str] = mapped_column(String(200), default="")

    @property
    def gross(self):
        return self.base + self.allowance + self.overtime + self.commission + self.bonus - self.leave_deduction

    @property
    def net(self):
        return self.gross - self.epf_ee - self.socso_ee - self.eis_ee - self.pcb - self.deductions

    @property
    def employer_cost(self):
        return self.gross + self.epf_er + self.socso_er + self.eis_er


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True)
    sup_type: Mapped[str] = mapped_column(String(30), default="Supplier")  # Supplier / Contractor
    bank_name: Mapped[str] = mapped_column(String(80), default="")
    account_no: Mapped[str] = mapped_column(String(40), default="")
    account_holder: Mapped[str] = mapped_column(String(150), default="")
    contact_person: Mapped[str] = mapped_column(String(100), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    email: Mapped[str] = mapped_column(String(100), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    tin: Mapped[str] = mapped_column(String(30), default="")     # LHDN Tax ID (e-Invoice)
    brn: Mapped[str] = mapped_column(String(30), default="")     # Business Registration No.


class BankAccount(Base):
    """A company bank account used for reconciliation (may run several)."""
    __tablename__ = "bank_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)   # e.g. "Maybank Current"
    bank_name: Mapped[str] = mapped_column(String(80), default="")
    account_no: Mapped[str] = mapped_column(String(40), default="")
    opening_balance: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class BankStatementLine(Base):
    """One imported line from a bank statement, to be matched against a system record."""
    __tablename__ = "bank_statement_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_accounts.id"))
    date: Mapped[date] = mapped_column(Date, default=date.today)
    description: Mapped[str] = mapped_column(Text, default="")
    ref: Mapped[str] = mapped_column(String(80), default="")
    amount: Mapped[float] = mapped_column(Float, default=0.0)   # +credit(in) / -debit(out)
    matched: Mapped[bool] = mapped_column(Boolean, default=False)
    matched_type: Mapped[str] = mapped_column(String(30), default="")   # Voucher/Sale/PettyCash/Manual
    matched_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_note: Mapped[str] = mapped_column(String(200), default="")
    import_batch: Mapped[str] = mapped_column(String(40), default="")
    # Posting a statement line directly to the ledger. Used for movements with
    # no source document in the system — historical months reconstructed from
    # statements, and ongoing bank charges or interest. Only ever set on
    # UNMATCHED lines: a matched line is already represented by its own record,
    # so posting it too would count the money twice.
    post_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True)
    account = relationship("BankAccount")
    post_account = relationship("Account", foreign_keys=[post_account_id])


SUPPLIER_TYPES = ["Supplier", "Contractor", "Service Provider", "Landlord", "Utility"]
MY_BANKS = ["Maybank", "CIMB Bank", "Public Bank", "RHB Bank", "Hong Leong Bank",
            "AmBank", "Bank Islam", "OCBC Bank", "UOB Bank", "Alliance Bank"]


class StatutoryPaid(Base):
    """Marks a monthly statutory remittance (EPF/SOCSO/EIS/PCB) as paid to the authority."""
    __tablename__ = "statutory_paid"
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(20))     # "Jul 2026"
    kind: Mapped[str] = mapped_column(String(20))       # EPF / SOCSO / EIS / PCB
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    paid_date: Mapped[date] = mapped_column(Date, default=date.today)
    paid_by: Mapped[str] = mapped_column(String(100), default="")


class ARInvoice(Base):
    """Customer invoice (receivable). Posts Dr Trade Debtors / Cr revenue on
    issue; receipts post Dr Bank(or Cash) / Cr Trade Debtors. Aging is
    computed from the due date (invoice date + 30 days when not given)."""
    __tablename__ = "ar_invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    inv_no: Mapped[str] = mapped_column(String(30), unique=True)
    customer: Mapped[str] = mapped_column(String(120))
    cust_address: Mapped[str] = mapped_column(Text, default="")
    cust_contact: Mapped[str] = mapped_column(String(60), default="")
    pdf_path: Mapped[str] = mapped_column(String(200), default="")
    stream: Mapped[str] = mapped_column(String(30), default="Boarding")
    date: Mapped[date] = mapped_column(Date, default=date.today)
    # No default: the route always supplies it (invoice date + 30 when blank).
    # (`date` here is the column above, not the datetime type — careful.)
    due_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    month: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(20), default="Open")   # Open / Paid / Void
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    receipts = relationship("ARReceipt", backref="invoice", cascade="all, delete-orphan")
    # One row per thing sold. Each line carries its own revenue stream, so a
    # boarding stay with grooming on top credits both accounts instead of
    # filing the grooming under boarding. Invoices made before lines existed
    # have none and fall back to amount + stream + notes.
    lines = relationship("ARInvoiceLine", backref="invoice", cascade="all, delete-orphan",
                         order_by="ARInvoiceLine.id")
    events = relationship("ARInvoiceEvent", backref="invoice", cascade="all, delete-orphan",
                          order_by="ARInvoiceEvent.id")

    @property
    def received(self):
        return round(sum(r.amount for r in self.receipts), 2)

    @property
    def outstanding(self):
        return round(self.amount - self.received, 2)


class ARInvoiceLine(Base):
    __tablename__ = "ar_invoice_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ar_invoices.id"))
    description: Mapped[str] = mapped_column(Text, default="")
    qty: Mapped[float] = mapped_column(Float, default=1.0)
    per: Mapped[str] = mapped_column(String(20), default="")      # night / cat / plan ...
    unit_price: Mapped[float] = mapped_column(Float, default=0.0) # negative = discount
    stream: Mapped[str] = mapped_column(String(30), default="Other")

    @property
    def amount(self) -> float:
        return round(self.qty * self.unit_price, 2)

    @property
    def printed(self) -> str:
        """The text on the invoice. Quantity and rate are spelled out only when
        there is more than one, so '35 nights x RM48' shows but '1 x RM99'
        doesn't clutter a single grooming."""
        if abs(self.qty - 1) < 1e-9:
            return self.description
        q = f"{self.qty:g}"
        unit = f" {self.per}{'s' if self.per and self.qty != 1 else ''}" if self.per else ""
        return f"{self.description} ({q}{unit} × RM{self.unit_price:,.2f})"


class ARInvoiceEvent(Base):
    """Append-only history of one invoice: created, edited (old → new),
    payment recorded or undone, voided. An invoice can be edited until money
    is recorded against it, so what it said before an edit has to survive."""
    __tablename__ = "ar_invoice_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ar_invoices.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    who: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(30), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


class ARReceipt(Base):
    __tablename__ = "ar_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ar_invoices.id"))
    date: Mapped[date] = mapped_column(Date, default=date.today)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[str] = mapped_column(String(20), default="Bank")   # Bank / Cash
    notes: Mapped[str] = mapped_column(String(200), default="")
    recorded_by: Mapped[str] = mapped_column(String(100), default="")


ACCOUNT_TYPES = ["Asset", "Liability", "Equity", "Income", "COGS", "Expense"]

# Account-code constants — SQL Account numbering per Weng Teng's template
# (VLIFE GREEN chart, Aug 2026). Use these, never raw code strings, so the
# next renumbering is one edit here instead of a hunt through the codebase.
ACC_CAPITAL = "100-000"        # Share / owner's capital
ACC_RETAINED = "150-000"       # Retained earning
ACC_OBE = "190-000"            # Opening balance equity (plug account)
ACC_RENOVATION = "200-600"     # Renovation (WIP)
ACC_ACCUM_DEPRN = "200-605"    # Accum. deprn. — renovation
ACC_EQUIPMENT = "200-700"      # Furniture & equipment
ACC_PROVISIONAL_FA = "200-800" # Provisional fixed-asset candidates
ACC_AR = "300-000"             # Trade debtors
ACC_BANK = "310-000"           # Cash at bank
ACC_CASH = "320-000"           # Cash in hand
ACC_TNG_CLEARING = "320-T01"   # Cash & TNG clearing (unverified)
ACC_PETTY = "325-000"          # Petty cash
ACC_STOCK = "330-000"          # Stock
ACC_DEPOSITS = "340-000"       # Deposit & prepayment
ACC_AP = "400-000"             # Trade creditors
ACC_ACCR_SALARY = "410-A01"    # Accrual — salary
ACC_EPF = "410-A02"            # Accrual — EPF
ACC_SOCSO_EIS = "410-A03"      # Accrual — SOCSO & EIS (template combines them)
ACC_PCB = "410-A04"            # Accrual — PCB
ACC_OTHER_DED = "410-A05"      # Accrual — other payroll deductions
ACC_SST = "420-000"            # SST payable
ACC_DEFERRED = "430-000"       # Deferred revenue
ACC_DIRECTOR = "440-000"       # Amount owing to director
ACC_FUNDING_SUSP = "490-000"   # Unclassified funding suspense
ACC_OFFBANK_SUSP = "495-000"   # Off-bank CAPEX funding suspense
ACC_OTHER_INCOME = "530-000"
ACC_MISC = "900-S03"           # Sundry / miscellaneous expenses
ACC_SALARIES = "900-S10"
ACC_EMPLOYER_STAT = "900-S20"
ACC_TAXATION = "950-000"

# Seed Chart of Accounts. code, name, type.
COA_SEED = [
    (ACC_CAPITAL, "Share Capital", "Equity"),
    (ACC_RETAINED, "Retained Earning", "Equity"),
    (ACC_OBE, "Opening Balance Equity", "Equity"),
    (ACC_RENOVATION, "Renovation", "Asset"),
    (ACC_ACCUM_DEPRN, "Accum. Deprn. — Renovation", "Asset"),
    (ACC_EQUIPMENT, "Furniture & Equipment", "Asset"),
    (ACC_PROVISIONAL_FA, "Fixed Assets — Provisional Candidates", "Asset"),
    (ACC_AR, "Trade Debtors", "Asset"),
    (ACC_BANK, "Cash at Bank", "Asset"),
    (ACC_CASH, "Cash in Hand", "Asset"),
    (ACC_TNG_CLEARING, "Cash & TNG Clearing (Unverified)", "Asset"),
    (ACC_PETTY, "Petty Cash", "Asset"),
    (ACC_STOCK, "Stock", "Asset"),
    (ACC_DEPOSITS, "Deposit & Prepayment", "Asset"),
    (ACC_AP, "Trade Creditors", "Liability"),
    (ACC_ACCR_SALARY, "Accrual — Salary", "Liability"),
    (ACC_EPF, "EPF Payable", "Liability"),
    (ACC_SOCSO_EIS, "SOCSO & EIS Payable", "Liability"),
    (ACC_PCB, "PCB Payable", "Liability"),
    (ACC_OTHER_DED, "Other Payroll Deductions Payable", "Liability"),
    (ACC_SST, "SST Payable", "Liability"),
    (ACC_DEFERRED, "Deferred Revenue", "Liability"),
    (ACC_DIRECTOR, "Amount Owing to Director", "Liability"),
    (ACC_FUNDING_SUSP, "Unclassified Funding Suspense", "Liability"),
    (ACC_OFFBANK_SUSP, "Off-Bank CAPEX Funding Suspense", "Liability"),
    ("500-001", "Boarding Revenue", "Income"),
    ("500-002", "Grooming Revenue", "Income"),
    ("500-003", "Cat Sales Revenue", "Income"),
    ("500-004", "Membership Revenue", "Income"),
    ("500-005", "Retail Revenue", "Income"),
    (ACC_OTHER_INCOME, "Other Income", "Income"),
    ("610-P01", "Purchases — Cat Supplies", "COGS"),
    ("610-P02", "Purchases — Grooming Supplies", "COGS"),
    ("610-P03", "Purchases — Vet & Medical", "COGS"),
    ("900-A04", "Admin & Office", "Expense"),
    ("900-D04", "Depreciation", "Expense"),
    ("900-I03", "Insurance", "Expense"),
    ("900-M03", "Marketing", "Expense"),
    ("900-R01", "Rental", "Expense"),
    ("900-S02", "Software & Subscriptions", "Expense"),
    (ACC_MISC, "Sundry Expenses", "Expense"),
    ("900-S05", "Staff Welfare", "Expense"),
    ("900-S08", "Staff Claims", "Expense"),
    (ACC_SALARIES, "Salaries & Wages", "Expense"),
    (ACC_EMPLOYER_STAT, "Employer Statutory (EPF/SOCSO/EIS)", "Expense"),
    ("900-T04", "Transport & Travelling", "Expense"),
    ("900-U03", "Repairs & Maintenance", "Expense"),
    ("900-U07", "Utilities (Water & Electricity)", "Expense"),
    (ACC_TAXATION, "Taxation", "Expense"),
]

# One-time migration: old 4-digit code → SQL Account code. Existing account
# rows are renamed IN PLACE (same row id) so every journal line survives.
# 2230 EIS Payable is handled separately — merged into 410-A03 SOCSO & EIS.
COA_RECODE = {
    "1010": ACC_CASH, "1015": ACC_TNG_CLEARING, "1020": ACC_BANK,
    "1030": ACC_PETTY, "1100": ACC_AR, "1200": ACC_STOCK, "1300": ACC_DEPOSITS,
    "1600": ACC_RENOVATION, "1610": ACC_EQUIPMENT, "1620": ACC_PROVISIONAL_FA,
    "1690": ACC_ACCUM_DEPRN,
    "2100": ACC_AP, "2210": ACC_EPF, "2220": ACC_SOCSO_EIS, "2240": ACC_PCB,
    "2250": ACC_OTHER_DED, "2300": ACC_SST, "2400": ACC_DEFERRED,
    "2900": ACC_FUNDING_SUSP, "2910": ACC_OFFBANK_SUSP,
    "3100": ACC_CAPITAL, "3200": ACC_RETAINED, "3900": ACC_OBE,
    "4010": "500-001", "4020": "500-002", "4030": "500-003",
    "4040": "500-004", "4050": "500-005", "4090": ACC_OTHER_INCOME,
    "5010": "610-P01", "5020": "610-P02", "5030": "610-P03",
    "6010": "900-R01", "6020": "900-U07", "6030": "900-M03", "6040": "900-I03",
    "6050": "900-S02", "6060": "900-T04", "6070": "900-A04", "6080": "900-U03",
    "6090": "900-S05", "6100": "900-S08", "6110": ACC_MISC,
    "6200": ACC_SALARIES, "6210": ACC_EMPLOYER_STAT, "6900": "900-D04",
}

# Payment/petty-cash category → account code
CATEGORY_ACCOUNT = {
    "Renovation": ACC_RENOVATION, "Equipment": ACC_EQUIPMENT,
    "Cat Supplies": "610-P01", "Grooming Supplies": "610-P02", "Vet": "610-P03",
    "Rental": "900-R01", "Utilities": "900-U07", "Marketing": "900-M03",
    "Insurance": "900-I03", "Software": "900-S02", "Transport": "900-T04",
    "Admin": "900-A04", "Maintenance": "900-U03", "Staff Welfare": "900-S05",
    "Staff Claim": "900-S08", "Misc": ACC_MISC, "Salary": ACC_SALARIES,
}
# Sales stream → income account code
STREAM_ACCOUNT = {
    "Boarding": "500-001", "Grooming": "500-002", "Cat Sales": "500-003",
    "Membership": "500-004", "Retail": "500-005", "Other": ACC_OTHER_INCOME,
}


class Account(Base):
    """Chart of Accounts."""
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(20))            # Asset/Liability/Equity/Income/COGS/Expense
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)  # seeded; posting rules depend on it
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class JournalEntry(Base):
    """One balanced double-entry posting. Auto entries are DERIVED from source
    records by app/ledger.py (idempotent, rebuildable); manual entries
    (opening balances, adjustments) persist."""
    __tablename__ = "journal_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, default=date.today)
    ref: Mapped[str] = mapped_column(String(30), default="")     # PAY-0009 / PV-0001 / MJE-0001…
    memo: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(30), default="Manual")  # Payment/Voucher/Sale/PettyCash/Payroll/Statutory/Opening/Manual
    source_id: Mapped[int] = mapped_column(Integer, default=0)
    event: Mapped[str] = mapped_column(String(20), default="")   # accrue / pay / post
    month: Mapped[str] = mapped_column(String(20), default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    lines = relationship("JournalLine", backref="entry", cascade="all, delete-orphan")

    @property
    def is_manual(self):
        return self.source_type in ("Manual", "Opening")


class JournalLine(Base):
    __tablename__ = "journal_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    debit: Mapped[float] = mapped_column(Float, default=0.0)
    credit: Mapped[float] = mapped_column(Float, default=0.0)
    description: Mapped[str] = mapped_column(String(200), default="")
    account = relationship("Account")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Counter(Base):
    __tablename__ = "counters"
    name: Mapped[str] = mapped_column(String(20), primary_key=True)  # DOC/PAY/PV/PL
    value: Mapped[int] = mapped_column(Integer, default=1)
