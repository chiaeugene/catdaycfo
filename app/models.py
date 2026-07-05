from datetime import datetime, date
from sqlalchemy import String, Integer, Float, Date, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

# ── Constants ────────────────────────────────────────────────────────────────
ROLES = ["admin", "manager", "staff"]

CATEGORIES = [
    "Renovation", "Equipment", "Cat Supplies", "Grooming Supplies", "Utilities",
    "Rental", "Salary", "Marketing", "Insurance", "Software", "Transport",
    "Admin", "Maintenance", "Staff Welfare", "Vet", "Misc",
]
GROUPS = ["CAPEX", "OPEX", "COGS", "Payroll", "Petty Cash"]
DOC_TYPES = ["Invoice", "Receipt", "Quotation", "Statement", "Bank-in Slip", "Payslip", "Other"]
# Section = where a verified document is routed
DOC_SECTIONS = ["Purchase", "Expense", "Petty Cash", "Bank-in Slip", "Payroll", "Filing Only"]
DOC_STATUS = ["Pending", "Verified", "Rejected"]
PAY_STATUS = ["Unsorted", "Categorized", "On Voucher", "Paid"]
PV_STATUS = ["Draft", "Approved", "Paid", "Void"]
PL_STATUS = ["Draft", "Submitted", "Processed"]
STREAMS = ["Boarding", "Grooming", "Cat Sales", "Membership", "Retail", "Other"]
PAY_METHODS = ["Cash", "Bank Transfer", "Card", "TNG", "Cheque"]


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default="staff")
    telegram_id: Mapped[str] = mapped_column(String(30), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


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
    file_path: Mapped[str] = mapped_column(String(300))            # relative to uploads/
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
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), default="")
    grp: Mapped[str] = mapped_column(String(30), default="")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
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


class PettyCashEntry(Base):
    __tablename__ = "petty_cash"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, default=date.today)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(50), default="")
    amount_out: Mapped[float] = mapped_column(Float, default=0.0)
    amount_in: Mapped[float] = mapped_column(Float, default=0.0)
    month: Mapped[str] = mapped_column(String(20), default="")
    recorded_by: Mapped[str] = mapped_column(String(100), default="")
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    document = relationship("Document")


class SalesEntry(Base):
    __tablename__ = "sales"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, default=date.today)
    stream: Mapped[str] = mapped_column(String(30), default="Boarding")
    description: Mapped[str] = mapped_column(Text, default="")
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[str] = mapped_column(String(30), default="Cash")
    month: Mapped[str] = mapped_column(String(20), default="")
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
    bonus: Mapped[float] = mapped_column(Float, default=0.0)
    epf_er: Mapped[float] = mapped_column(Float, default=0.0)
    epf_ee: Mapped[float] = mapped_column(Float, default=0.0)
    socso_er: Mapped[float] = mapped_column(Float, default=0.0)
    socso_ee: Mapped[float] = mapped_column(Float, default=0.0)
    eis_er: Mapped[float] = mapped_column(Float, default=0.0)
    eis_ee: Mapped[float] = mapped_column(Float, default=0.0)
    deductions: Mapped[float] = mapped_column(Float, default=0.0)   # other deductions
    remarks: Mapped[str] = mapped_column(String(200), default="")

    @property
    def gross(self):
        return self.base + self.allowance + self.overtime + self.bonus

    @property
    def net(self):
        return self.gross - self.epf_ee - self.socso_ee - self.eis_ee - self.deductions

    @property
    def employer_cost(self):
        return self.gross + self.epf_er + self.socso_er + self.eis_er


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Counter(Base):
    __tablename__ = "counters"
    name: Mapped[str] = mapped_column(String(20), primary_key=True)  # DOC/PAY/PV/PL
    value: Mapped[int] = mapped_column(Integer, default=1)
