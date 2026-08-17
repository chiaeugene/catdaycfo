from datetime import datetime, date
from sqlalchemy import String, Integer, Float, Date, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

# ── Constants ────────────────────────────────────────────────────────────────
# "viewer" is read-only system-wide — enforced centrally in app/audit.py's
# AccessControlMiddleware, not by individual routes (many older routes never
# had their own role check, so a per-route approach would miss some).
ROLES = ["admin", "manager", "staff", "viewer"]

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
DOC_TYPES = ["Invoice", "Receipt", "Quotation", "Statement", "Bank-in Slip", "Payslip", "Other"]
# Section = where a verified submission is routed
DOC_SECTIONS = ["Purchase", "Expense", "Staff Claim", "Petty Cash", "Sales Report",
                "Boarding Log", "Bank-in Slip", "Payroll", "Filing Only"]
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
    account = relationship("BankAccount")


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

    @property
    def received(self):
        return round(sum(r.amount for r in self.receipts), 2)

    @property
    def outstanding(self):
        return round(self.amount - self.received, 2)


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
