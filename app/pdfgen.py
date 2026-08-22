"""PDF generation in the cat day brand system: Sales Invoices, Payment
Vouchers, Payment Listings, Payslips.

Brand Guidelines (Aug 2026): Gliker for the logotype and display type, Onest
for body text, and the five brand colours. The logo is reversed out of a
full-bleed brand-brown header band, per the guide's primary logo-usage rule
("if background is dark in colour, the logo appears in #ECDBB6").

Every document shares the same skeleton — brand band, Gliker title, meta
block, brand table, cream boxes, signature rules, footer — so they read as one
family. Layout constants are defined once (PAGE_W / MARGIN / CONTENT_W) and
everything aligns to the same left and right edges.

All PDF text is English-only: the brand fonts, like Helvetica before them,
have no CJK coverage, so Chinese characters would render as blank boxes.
File names follow: INV-2608-001_Wendy-Chee.pdf / PV-0001_TNJ-Design.pdf /
PL-2608-001_2026-08-22.pdf / PAYSLIP_Jul-2026_Karen.pdf
"""
import os
import re
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable, Image as RLImage)
from reportlab.lib.styles import ParagraphStyle

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")

# ── Brand colours — verbatim from the guide's BRAND COLOR CODE page ──────────
BRAND_BROWN = colors.HexColor("#2D1907")   # primary dark — header bands, logo ground
BRAND_CREAM = colors.HexColor("#ECDBB6")   # logo on dark grounds; box headers, totals
BRAND_YELLOW = colors.HexColor("#E7CE7A")  # warm accent
BRAND_RUST = colors.HexColor("#B14919")    # burnt orange — emphasis, amounts due
BRAND_TEAL = colors.HexColor("#729094")    # steel teal — secondary/muted text
CREAM_TINT = colors.HexColor("#FAF5EA")    # near-white cream for row banding
RULE = colors.HexColor("#E8E0D2")          # hairline rules inside tables

TAGLINE = "a good day for every cat."

# ── Layout: one set of constants so every document aligns identically ────────
PAGE_W = 210 * mm
MARGIN = 17 * mm
CONTENT_W = PAGE_W - 2 * MARGIN          # 176mm — the width every table spans

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_CREAM = os.path.join(ASSETS, "brand", "catday-logo-cream.png")
LOGO_RATIO = 2.36                        # width / height of the horizontal lockup


def _register_brand_fonts():
    """Register Gliker (display) and Onest (body). Returns
    (display, body, bold, medium), falling back to Helvetica if the files are
    missing so PDF generation never hard-fails in an environment without them."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    fdir = os.path.join(ASSETS, "fonts")
    want = [("Gliker-Bold", "Gliker-Bold.ttf"), ("Onest", "Onest-Regular.ttf"),
            ("Onest-Bold", "Onest-Bold.ttf"), ("Onest-Medium", "Onest-Medium.ttf")]
    try:
        for name, fn in want:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, os.path.join(fdir, fn)))
        return "Gliker-Bold", "Onest", "Onest-Bold", "Onest-Medium"
    except Exception:
        return "Helvetica-Bold", "Helvetica", "Helvetica-Bold", "Helvetica"


DISPLAY_F, BODY_F, BOLD_F, MED_F = _register_brand_fonts()

# ── Shared paragraph styles ──────────────────────────────────────────────────
LBL = ParagraphStyle("lbl", fontName=BODY_F, fontSize=7.5, textColor=BRAND_TEAL, leading=10.5)
LBL_R = ParagraphStyle("lblr", parent=LBL, alignment=2)
VAL = ParagraphStyle("val", fontName=BOLD_F, fontSize=10.5, textColor=BRAND_BROWN, leading=13.5)
VAL_R = ParagraphStyle("valr", parent=VAL, alignment=2)
BODY = ParagraphStyle("body", fontName=BODY_F, fontSize=9.5, textColor=BRAND_BROWN, leading=13.5)
BODY_R = ParagraphStyle("bodyr", parent=BODY, alignment=2)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=9, leading=13)
SMALL_R = ParagraphStyle("smallr", parent=SMALL, alignment=2)
TINY = ParagraphStyle("tiny", fontName=BODY_F, fontSize=7.5, textColor=BRAND_TEAL, leading=11)
TH = ParagraphStyle("th", fontName=BOLD_F, fontSize=8.5, textColor=BRAND_CREAM, leading=11.5)
TH_R = ParagraphStyle("thr", parent=TH, alignment=2)
TOT = ParagraphStyle("tot", fontName=BOLD_F, fontSize=10, textColor=BRAND_BROWN, leading=13)
TOT_R = ParagraphStyle("totr", parent=TOT, alignment=2)
RUSTY = ParagraphStyle("rust", fontName=BOLD_F, fontSize=11, textColor=BRAND_RUST, leading=14)
RUSTY_R = ParagraphStyle("rustr", parent=RUSTY, alignment=2)
BOXTITLE = ParagraphStyle("bt", fontName=BOLD_F, fontSize=7.5, textColor=BRAND_RUST, leading=10)


def safe_name(s: str, maxlen: int = 40) -> str:
    """Filesystem-safe slug: 'TNJ Design Sdn Bhd' -> 'TNJ-Design-Sdn-Bhd'."""
    s = re.sub(r"[^\w\s-]", "", s or "").strip()
    s = re.sub(r"[\s_]+", "-", s)
    return (s or "unnamed")[:maxlen]


def _doc(rel_path: str):
    return SimpleDocTemplate(os.path.join(UPLOAD_DIR, rel_path), pagesize=A4,
                             topMargin=0, bottomMargin=14 * mm,
                             leftMargin=MARGIN, rightMargin=MARGIN)


def _brand_band(company: str, address: str = "", reg_no: str = ""):
    """Full-bleed brand-brown header: logo left, legal entity right. The band
    is PAGE_W wide with MARGIN padding, so its content sits on exactly the same
    left/right edges as every table below it."""
    if os.path.exists(LOGO_CREAM):
        mark = RLImage(LOGO_CREAM, width=44 * mm, height=44 / LOGO_RATIO * mm)
        mark.hAlign = "LEFT"
    else:
        mark = Paragraph("cat day", ParagraphStyle(
            "bn", fontName=DISPLAY_F, fontSize=26, textColor=BRAND_CREAM))
    entity = Paragraph(
        f'<font name="{BOLD_F}" size=9.5 color="#ECDBB6">{company}</font><br/>'
        + (f'<font size=8 color="#E7CE7A">Company No. {reg_no}</font><br/>' if reg_no else "")
        + (f'<font size=8 color="#ECDBB6">{address}</font>' if address else ""),
        ParagraphStyle("ent", fontName=BODY_F, fontSize=8, leading=12,
                       alignment=2, textColor=BRAND_CREAM))
    band = Table([[mark, entity]], colWidths=[80 * mm, 130 * mm])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND_BROWN),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), MARGIN),
        ("RIGHTPADDING", (0, 0), (0, 0), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 0),
        ("RIGHTPADDING", (1, 0), (1, 0), MARGIN),
        ("TOPPADDING", (0, 0), (-1, -1), 11 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 11 * mm),
    ]))
    band.hAlign = "CENTER"   # 210mm table in a 176mm frame → bleeds to both edges
    return band


def _title(text: str):
    return Paragraph(text, ParagraphStyle(
        "title", fontName=DISPLAY_F, fontSize=18, textColor=BRAND_BROWN, leading=21))


def _meta_block(pairs, col_widths=None):
    """Label/value header block. The FIRST field is left-aligned (the party the
    document concerns); the rest are right-aligned so the final column lands on
    the content's right edge, flush with the amount column of the table below."""
    n = len(pairs)
    if col_widths is None:
        first = CONTENT_W - (n - 1) * 34 * mm
        col_widths = [first] + [34 * mm] * (n - 1)
    labels, values = [], []
    for i, (lab, val, *rest) in enumerate(pairs):
        style_v = rest[0] if rest else (VAL if i == 0 else VAL_R)
        labels.append(Paragraph(lab, LBL if i == 0 else LBL_R))
        values.append(Paragraph(val, style_v))
    t = Table([labels, values], colWidths=col_widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
    ]))
    return t


def _brand_table(data, col_widths, n_body_rows, total_rows=(), rust_rows=()):
    """Table in brand dress: brown header row, cream-tinted banding, cream
    total rows, and a rust rule above any emphasised row."""
    t = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_BROWN),
        ("ROWBACKGROUNDS", (0, 1), (-1, n_body_rows), [colors.white, CREAM_TINT]),
        ("LINEBELOW", (0, 1), (-1, n_body_rows - 1), 0.4, RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        # 6pt, not 8 — narrow index columns ("NO.") wrap their own header at 8.
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    for r in total_rows:
        style.append(("BACKGROUND", (0, r), (-1, r), BRAND_CREAM))
    for r in rust_rows:
        style += [("BACKGROUND", (0, r), (-1, r), BRAND_CREAM),
                  ("LINEABOVE", (0, r), (-1, r), 1.2, BRAND_RUST)]
    t.setStyle(TableStyle(style))
    return t


def _brand_box(title: str, rows, col_widths=None, header_row=None):
    """Cream-headed bordered box (payment details, schedules, contributions).
    `rows` are already-built cell contents; `header_row` is an optional column
    header line rendered under the title."""
    col_widths = col_widths or [34 * mm, CONTENT_W - 34 * mm]
    ncols = len(col_widths)
    data = [[Paragraph(title, BOXTITLE)] + [""] * (ncols - 1)]
    head_idx = None
    if header_row:
        data.append(header_row)
        head_idx = 1
    data.extend(rows)
    t = Table(data, colWidths=col_widths)
    style = [
        ("SPAN", (0, 0), (-1, 0)),
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_CREAM),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.7, BRAND_CREAM),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]
    if head_idx is not None:
        style += [("LINEBELOW", (0, head_idx), (-1, head_idx), 0.5, BRAND_CREAM),
                  ("VALIGN", (0, head_idx), (-1, -1), "MIDDLE")]
    t.setStyle(TableStyle(style))
    return t


def _sig_block(labels):
    """Signature rules with Onest captions. A spacer column sits between each
    pair — adjacent LINEBELOWs would otherwise run together as one long rule."""
    n = len(labels)
    gap = 12 * mm
    w = (CONTENT_W - gap * (n - 1)) / n
    widths, cells, caps = [], [], []
    for i, lab in enumerate(labels):
        if i:
            widths.append(gap)
            cells.append("")
            caps.append("")
        widths.append(w)
        cells.append("")
        caps.append(Paragraph(lab, TINY))
    t = Table([cells, caps], colWidths=widths, hAlign="LEFT")
    style = [
        ("TOPPADDING", (0, 0), (-1, 0), 18 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 1), (-1, 1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]
    for i in range(n):
        col = i * 2
        style.append(("LINEBELOW", (col, 0), (col, 0), 0.7, BRAND_CREAM))
    t.setStyle(TableStyle(style))
    return t


def _footer(el, company: str, reg_no: str, ref: str, note: str = ""):
    el.append(Spacer(1, 8 * mm))
    el.append(HRFlowable(width="100%", thickness=0.7, color=BRAND_CREAM))
    el.append(Spacer(1, 2 * mm))
    if note:
        el.append(Paragraph(note, TINY))
    el.append(Paragraph(
        f'<font name="{DISPLAY_F}" size=9 color="#B14919">{TAGLINE}</font>'
        f'&nbsp;&nbsp;&nbsp;{company}'
        + (f' &middot; Company No. {reg_no}' if reg_no else "")
        + (f' &middot; {ref}' if ref else ""), TINY))


# ════════════════════════════ SALES INVOICE ════════════════════════════
def invoice_pdf(inv_no: str, customer: str, cust_address: str, cust_contact: str,
                items: list[dict], due_date: str, notes: str = "",
                deposit_paid: float = 0.0, schedule: list[dict] | None = None,
                company="MEOW & ME PET SHOP SDN BHD", address="", reg_no="",
                bank: dict | None = None) -> str:
    """Customer sales invoice.

    `company` / `reg_no` carry the LEGAL entity — Malaysian invoices must show
    the registered name and company number even though the trading brand shown
    by the logo is cat day.

    Payment status is one of two mutually exclusive modes — never guess:
      - deposit_paid > 0: money already received, deducted from the total with
        the remaining balance highlighted.
      - schedule: nothing received yet — a Payment Schedule lists what is due
        and when, each row marked "Due". Use this when a deposit has been
        AGREED but not actually collected.
    """
    subdir = f"invoices/{date.today():%Y-%m}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel = f"{subdir}/{inv_no}_{safe_name(customer)}.pdf"
    doc = _doc(rel)
    el = [_brand_band(company, address, reg_no), Spacer(1, 9 * mm),
          _title("SALES INVOICE"), Spacer(1, 5 * mm)]

    el.append(_meta_block([
        ("BILL TO", customer),
        ("INVOICE NO.", inv_no),
        ("INVOICE DATE", f"{date.today():%d/%m/%Y}"),
        ("PAYMENT DUE", due_date, ParagraphStyle("duer", parent=VAL_R, textColor=BRAND_RUST)),
    ]))
    addr_bits = "<br/>".join(filter(None, [cust_address,
                                           f"Tel: {cust_contact}" if cust_contact else ""]))
    if addr_bits:
        el.append(Spacer(1, 1.5 * mm))
        el.append(Paragraph(addr_bits, SMALL))
    el.append(Spacer(1, 7 * mm))

    total = sum(it["amount"] for it in items)
    data = [[Paragraph("NO.", TH), Paragraph("DESCRIPTION", TH),
             Paragraph("AMOUNT (RM)", TH_R)]]
    for i, it in enumerate(items, 1):
        data.append([Paragraph(str(i), BODY), Paragraph(it["description"], BODY),
                     Paragraph(f"{it['amount']:,.2f}", BODY_R)])
    n_items = len(items)
    data.append(["", Paragraph("TOTAL", TOT), Paragraph(f"{total:,.2f}", TOT_R)])
    totals, rusts = [n_items + 1], []
    if deposit_paid:
        balance = round(total - deposit_paid, 2)
        data.append(["", Paragraph("Less: Deposit Received", BODY),
                     Paragraph(f"-{deposit_paid:,.2f}", BODY_R)])
        data.append(["", Paragraph("BALANCE DUE", RUSTY),
                     Paragraph(f"{balance:,.2f}", RUSTY_R)])
        totals.append(len(data) - 2)
        rusts = [len(data) - 1]
    el.append(_brand_table(data, [12 * mm, 124 * mm, 40 * mm], n_items, totals, rusts))
    el.append(Spacer(1, 6 * mm))

    if schedule:
        sh = ParagraphStyle("sh", fontName=BOLD_F, fontSize=8,
                            textColor=BRAND_BROWN, leading=11)
        rows = [[Paragraph(s["label"], SMALL),
                 Paragraph(f"{s['amount']:,.2f}", SMALL_R),
                 Paragraph(s["due"], SMALL),
                 Paragraph(s.get("status", "Due"),
                           ParagraphStyle("st", parent=SMALL, fontName=BOLD_F,
                                          textColor=BRAND_RUST))]
                for s in schedule]
        el.append(_brand_box(
            "PAYMENT SCHEDULE", rows,
            col_widths=[44 * mm, 30 * mm, 52 * mm, 50 * mm],
            header_row=[Paragraph("PAYMENT", sh),
                        Paragraph("AMOUNT (RM)", ParagraphStyle("shr", parent=sh, alignment=2)),
                        Paragraph("DUE", sh), Paragraph("STATUS", sh)]))
        el.append(Spacer(1, 6 * mm))

    if bank and (bank.get("bank_name") or bank.get("account_no")):
        el.append(_brand_box("PAYMENT DETAILS", [
            [Paragraph("Bank", LBL), Paragraph(bank.get("bank_name") or "-", VAL)],
            [Paragraph("Account No.", LBL), Paragraph(bank.get("account_no") or "-", VAL)],
            [Paragraph("Account Holder", LBL),
             Paragraph(bank.get("account_holder") or company, VAL)],
        ]))
        el.append(Spacer(1, 5 * mm))

    if notes:
        el.append(Paragraph(
            f'<font name="{BOLD_F}" color="#B14919">Notes</font><br/>{notes}', SMALL))
        el.append(Spacer(1, 4 * mm))

    el.append(_sig_block(["Issued By", "Received By"]))
    _footer(el, company, reg_no, inv_no)
    doc.build(el)
    return rel


# ═══════════════════════════ PAYMENT VOUCHER ═══════════════════════════
def voucher_pdf(pv_no: str, payee: str, items: list[dict], total: float,
                company="CATDAY SDN BHD", address="Uptown PJ", reg_no="",
                bank: dict | None = None) -> str:
    """items: [{date, description, amount, invoice_no, doc_url}]. bank:
    {bank_name, account_no, account_holder} or None."""
    subdir = f"vouchers/{date.today():%Y-%m}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel = f"{subdir}/{pv_no}_{safe_name(payee)}.pdf"
    doc = _doc(rel)
    el = [_brand_band(company, address, reg_no), Spacer(1, 9 * mm),
          _title("PAYMENT VOUCHER"), Spacer(1, 5 * mm)]

    el.append(_meta_block([
        ("PAYEE", payee),
        ("VOUCHER NO.", pv_no),
        ("DATE", f"{date.today():%d/%m/%Y}"),
    ]))
    el.append(Spacer(1, 7 * mm))

    if bank and (bank.get("bank_name") or bank.get("account_no")):
        el.append(_brand_box("BANK TRANSFER DETAILS", [
            [Paragraph("Bank", LBL), Paragraph(bank.get("bank_name") or "-", VAL)],
            [Paragraph("Account No.", LBL), Paragraph(bank.get("account_no") or "-", VAL)],
            [Paragraph("Account Holder", LBL),
             Paragraph(bank.get("account_holder") or payee, VAL)],
        ]))
    else:
        el.append(Paragraph(
            "Bank details not on file — pay by cash/cheque, or add them in the "
            "supplier directory.", TINY))
    el.append(Spacer(1, 6 * mm))

    data = [[Paragraph("NO.", TH), Paragraph("DATE", TH), Paragraph("INVOICE NO.", TH),
             Paragraph("DESCRIPTION", TH), Paragraph("AMOUNT (RM)", TH_R)]]
    for i, it in enumerate(items, 1):
        inv = it.get("invoice_no") or "-"
        url = it.get("doc_url")
        inv_cell = Paragraph(
            f'<link href="{url}" color="#B14919"><u>{inv}</u></link>' if url else inv, SMALL)
        data.append([Paragraph(str(i), BODY), Paragraph(it["date"], BODY), inv_cell,
                     Paragraph(it["description"], BODY),
                     Paragraph(f"{it['amount']:,.2f}", BODY_R)])
    n = len(items)
    data.append(["", "", "", Paragraph("TOTAL", TOT), Paragraph(f"{total:,.2f}", TOT_R)])
    el.append(_brand_table(data, [13 * mm, 21 * mm, 30 * mm, 80 * mm, 32 * mm],
                           n, [n + 1]))
    if any(it.get("doc_url") for it in items):
        el.append(Spacer(1, 2 * mm))
        el.append(Paragraph("Invoice numbers are clickable — they open the original "
                            "document in the cat day system.", TINY))

    el.append(_sig_block(["Prepared By", "Approved By", "Received By"]))
    _footer(el, company, reg_no, pv_no)
    doc.build(el)
    return rel


# ═══════════════════════════ PAYMENT LISTING ═══════════════════════════
def listing_pdf(pl_no: str, vouchers: list[dict], total: float,
                company="CATDAY SDN BHD", address="Uptown PJ", reg_no="") -> str:
    """vouchers: [{pv_no, date, payee, total, bank}] where bank is a
    'Bank / Account No.' display string (may be empty)."""
    subdir = f"listings/{date.today():%Y-%m}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel = f"{subdir}/{pl_no}_{date.today():%Y-%m-%d}.pdf"
    doc = _doc(rel)
    el = [_brand_band(company, address, reg_no), Spacer(1, 9 * mm),
          _title("PAYMENT LISTING"), Spacer(1, 5 * mm)]

    el.append(_meta_block([
        ("PREPARED FOR", "Bank payment run"),
        ("LISTING NO.", pl_no),
        ("DATE", f"{date.today():%d/%m/%Y}"),
        ("VOUCHERS", str(len(vouchers))),
    ]))
    el.append(Spacer(1, 7 * mm))

    data = [[Paragraph("NO.", TH), Paragraph("PV NO.", TH), Paragraph("DATE", TH),
             Paragraph("PAYEE", TH), Paragraph("BANK / ACCOUNT NO.", TH),
             Paragraph("AMOUNT (RM)", TH_R)]]
    for i, v in enumerate(vouchers, 1):
        data.append([Paragraph(str(i), BODY), Paragraph(v["pv_no"], BODY),
                     Paragraph(v["date"], BODY), Paragraph(v["payee"], SMALL),
                     Paragraph(v.get("bank") or "-", SMALL),
                     Paragraph(f"{v['total']:,.2f}", BODY_R)])
    n = len(vouchers)
    data.append(["", "", "", "", Paragraph("GRAND TOTAL", TOT),
                 Paragraph(f"{total:,.2f}", TOT_R)])
    el.append(_brand_table(data, [13 * mm, 23 * mm, 20 * mm, 43 * mm, 45 * mm, 32 * mm],
                           n, [n + 1]))

    el.append(_sig_block(["Prepared By", "Approved By"]))
    _footer(el, company, reg_no, pl_no)
    doc.build(el)
    return rel


# ════════════════════════════════ PAYSLIP ═══════════════════════════════
def payslip_pdf(month: str, item, company="CATDAY SDN BHD", address="Uptown PJ",
                reg_no="") -> str:
    """item: PayrollItem."""
    subdir = f"payslips/{safe_name(month)}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel = f"{subdir}/PAYSLIP_{safe_name(month)}_{safe_name(item.staff_name)}.pdf"
    doc = _doc(rel)
    el = [_brand_band(company, address, reg_no), Spacer(1, 9 * mm),
          _title(f"PAYSLIP — {month.upper()}"), Spacer(1, 5 * mm)]

    el.append(_meta_block([
        ("EMPLOYEE", item.staff_name),
        ("POSITION", item.position or "-"),
        ("PAY PERIOD", month),
        ("DATE", f"{date.today():%d/%m/%Y}"),
    ], col_widths=[62 * mm, 46 * mm, 34 * mm, 34 * mm]))
    el.append(Spacer(1, 7 * mm))

    def money_table(header, rows, total_label, total_amt):
        data = [[Paragraph(header, TH), Paragraph("RM", TH_R)]]
        for lab, amt in rows:
            data.append([Paragraph(lab, SMALL), Paragraph(f"{amt:,.2f}", SMALL_R)])
        n = len(rows)
        data.append([Paragraph(total_label, TOT), Paragraph(f"{total_amt:,.2f}", TOT_R)])
        return _brand_table(data, [52 * mm, 30 * mm], n, [n + 1])

    earn = [("Basic Salary", item.base)]
    for lab, amt in [("Allowance", item.allowance), ("Overtime", item.overtime),
                     ("Commission", item.commission), ("Bonus", item.bonus)]:
        if amt:
            earn.append((lab, amt))
    if item.leave_deduction:
        earn.append((f"Less: Unpaid Leave ({item.unpaid_leave_days:g}d)", -item.leave_deduction))

    ded = [("EPF (Employee)", item.epf_ee), ("SOCSO (Employee)", item.socso_ee),
           ("EIS (Employee)", item.eis_ee)]
    if item.pcb:
        ded.append(("PCB / MTD (Tax)", item.pcb))
    if item.deductions:
        ded.append(("Other Deductions", item.deductions))
    total_ded = item.epf_ee + item.socso_ee + item.eis_ee + item.pcb + item.deductions

    pair = Table([[money_table("EARNINGS", earn, "Gross Pay", item.gross),
                   money_table("DEDUCTIONS", ded, "Total Deductions", total_ded)]],
                 colWidths=[86 * mm, 90 * mm])
    pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (0, 0), 0),
                              ("RIGHTPADDING", (0, 0), (0, 0), 4 * mm),
                              ("LEFTPADDING", (1, 0), (1, 0), 0),
                              ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
    el.append(pair)
    el.append(Spacer(1, 6 * mm))

    net = Table([[Paragraph("NET PAY", ParagraphStyle(
        "np", fontName=BOLD_F, fontSize=12, textColor=BRAND_CREAM, leading=15)),
        Paragraph(f"RM {item.net:,.2f}", ParagraphStyle(
            "npv", fontName=BOLD_F, fontSize=12, textColor=BRAND_CREAM,
            leading=15, alignment=2))]],
        colWidths=[CONTENT_W - 50 * mm, 50 * mm])
    net.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND_BROWN),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    el.append(net)
    el.append(Spacer(1, 6 * mm))

    er_rows = [[Paragraph(lab, SMALL), Paragraph(f"{amt:,.2f}", SMALL_R)]
               for lab, amt in [("EPF (Employer)", item.epf_er),
                                ("SOCSO (Employer)", item.socso_er),
                                ("EIS (Employer)", item.eis_er),
                                ("Total Employer Cost", item.employer_cost)]]
    el.append(_brand_box("EMPLOYER CONTRIBUTIONS — NOT DEDUCTED FROM PAY", er_rows,
                         col_widths=[CONTENT_W - 40 * mm, 40 * mm]))

    if item.remarks:
        el.append(Spacer(1, 4 * mm))
        el.append(Paragraph(f'<font name="{BOLD_F}" color="#B14919">Remarks</font><br/>'
                            f'{item.remarks}', SMALL))

    el.append(_sig_block(["Employer", "Employee"]))
    _footer(el, company, reg_no, f"{month} · {item.staff_name}",
            note="This is a computer-generated payslip.")
    doc.build(el)
    return rel
