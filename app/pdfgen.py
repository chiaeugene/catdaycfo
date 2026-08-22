"""PDF generation: Payment Vouchers, Payment Listings, Payslips.

All PDF text is English-only — reportlab's built-in Helvetica cannot render
Chinese characters or emoji (they appear as black boxes).
File names follow: PV-0001_TNJ-Design.pdf / PL-0001_2026-07-05.pdf /
PAYSLIP_Jul-2026_Karen.pdf
"""
import os
import re
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
ORANGE = colors.HexColor("#7F2F01")
ORANGE_MID = colors.HexColor("#A6450F")
LIGHT = colors.HexColor("#FCE4D6")
GRAY = colors.HexColor("#666666")

# ── cat day brand system (Brand Guidelines, Aug 2026) ────────────────────────
# The five brand colours, verbatim from the guide's BRAND COLOR CODE page.
BRAND_BROWN = colors.HexColor("#2D1907")   # primary dark — headers, logo ground
BRAND_CREAM = colors.HexColor("#ECDBB6")   # logo on dark grounds; soft fills
BRAND_YELLOW = colors.HexColor("#E7CE7A")  # warm accent
BRAND_RUST = colors.HexColor("#B14919")    # burnt orange — emphasis, amounts due
BRAND_TEAL = colors.HexColor("#729094")    # steel teal — secondary/muted text
BRAND_CREAM_TINT = colors.HexColor("#FAF5EA")   # near-white cream for row banding

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_CREAM = os.path.join(ASSETS, "brand", "catday-logo-cream.png")


def _register_brand_fonts():
    """Register the brand typefaces — Gliker (logotype/display) and Onest
    (body). Returns (display_font, body, body_bold, body_medium), falling back
    to Helvetica if the files are missing so PDFs still generate anywhere."""
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


DISPLAY_F, BODY_F, BODY_BOLD_F, BODY_MED_F = _register_brand_fonts()

styles = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=styles["Title"], textColor=ORANGE, fontSize=17, spaceAfter=1)
H2 = ParagraphStyle("h2", parent=styles["Heading2"], alignment=1, spaceBefore=4, textColor=ORANGE_MID)
SMALL = ParagraphStyle("small", parent=styles["Normal"], fontSize=9)
TINY = ParagraphStyle("tiny", parent=styles["Normal"], fontSize=7.5, textColor=GRAY)
BANKVAL = ParagraphStyle("bankval", parent=styles["Normal"], fontSize=10, fontName="Helvetica-Bold")
BANKCELL = ParagraphStyle("bankcell", parent=styles["Normal"], fontSize=10)


def safe_name(s: str, maxlen: int = 40) -> str:
    """Filesystem-safe slug: 'TNJ Design Sdn Bhd' -> 'TNJ-Design-Sdn-Bhd'."""
    s = re.sub(r"[^\w\s-]", "", s or "").strip()
    s = re.sub(r"[\s_]+", "-", s)
    return (s or "unnamed")[:maxlen]


def _header(el, company, address, doc_title, reg_no=""):
    el.append(Paragraph(company, H1))
    if reg_no:
        el.append(Paragraph(f"Company No.: {reg_no}", SMALL))
    if address:
        el.append(Paragraph(address, SMALL))
    el.append(Spacer(1, 2 * mm))
    el.append(HRFlowable(width="100%", thickness=1.2, color=ORANGE))
    el.append(Paragraph(doc_title, H2))
    el.append(Spacer(1, 4 * mm))


def _bank_block(title: str, bank: dict, fallback_holder: str):
    """Bank transfer / payment details box. All cell content is wrapped in
    Paragraphs (not raw strings) so long values — bank names, and especially
    long company account-holder names — wrap inside their column instead of
    overflowing past the box border."""
    holder = bank.get("account_holder") or fallback_holder
    bt = Table([
        [title, "", ""],
        [Paragraph("Bank:", BANKCELL), Paragraph(bank.get("bank_name") or "-", BANKVAL),
         Paragraph(f"Account Holder: {holder}", BANKCELL)],
        [Paragraph("Account No.:", BANKCELL), Paragraph(bank.get("account_no") or "-", BANKVAL), ""],
    ], colWidths=[28 * mm, 60 * mm, 82 * mm])
    bt.setStyle(TableStyle([
        ("SPAN", (0, 0), (-1, 0)),
        ("BACKGROUND", (0, 0), (-1, 0), LIGHT),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), ORANGE),
        ("TEXTCOLOR", (0, 1), (0, -1), GRAY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.8, ORANGE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return bt


def _sig_block(labels):
    cells = [["_" * 28 for _ in labels], labels]
    t = Table(cells, colWidths=[170 * mm / len(labels)] * len(labels), hAlign="CENTER")
    t.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, 0), 36),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    return t


def _grid_style(last_bold=True):
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), ORANGE),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#FDF3EC")]),
    ]
    if last_bold:
        style += [
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), LIGHT),
        ]
    return TableStyle(style)


def voucher_pdf(pv_no: str, payee: str, items: list[dict], total: float,
                company="CATDAY SDN BHD", address="Uptown PJ",
                bank: dict | None = None) -> str:
    """items: [{date, description, amount}]. bank: {bank_name, account_no,
    account_holder} or None. Returns relative pdf path."""
    subdir = f"vouchers/{date.today():%Y-%m}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel = f"{subdir}/{pv_no}_{safe_name(payee)}.pdf"
    doc = SimpleDocTemplate(os.path.join(UPLOAD_DIR, rel), pagesize=A4,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    el = []
    _header(el, company, address, "PAYMENT VOUCHER")

    info = Table([
        ["Voucher No.:", pv_no, "Date:", f"{date.today():%d/%m/%Y}"],
        ["Payee:", payee, "", ""],
    ], colWidths=[30 * mm, 75 * mm, 20 * mm, 40 * mm])
    info.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), GRAY),
        ("TEXTCOLOR", (2, 0), (2, -1), GRAY),
    ]))
    el.append(info)
    el.append(Spacer(1, 4 * mm))

    if bank and (bank.get("bank_name") or bank.get("account_no")):
        el.append(_bank_block("BANK TRANSFER DETAILS", bank, payee))
    else:
        el.append(Paragraph("Bank details not on file - pay by cash/cheque or update the supplier directory.", TINY))
    el.append(Spacer(1, 6 * mm))

    data = [["No.", "Date", "Invoice No.", "Description", "Amount (RM)"]]
    for i, it in enumerate(items, 1):
        inv = it.get("invoice_no") or "-"
        url = it.get("doc_url")
        if url:
            # Clickable: opens the original invoice/receipt in the system (login required)
            inv_cell = Paragraph(f'<link href="{url}" color="#A6450F"><u>{inv}</u></link>', SMALL)
        else:
            inv_cell = Paragraph(inv, SMALL)
        data.append([str(i), it["date"], inv_cell, Paragraph(it["description"], SMALL), f"{it['amount']:,.2f}"])
    data.append(["", "", "", "TOTAL", f"{total:,.2f}"])
    t = Table(data, colWidths=[10 * mm, 20 * mm, 32 * mm, 78 * mm, 30 * mm], repeatRows=1)
    t.setStyle(_grid_style())
    t.setStyle(TableStyle([("ALIGN", (4, 0), (4, -1), "RIGHT")]))
    el.append(t)
    el.append(Spacer(1, 2 * mm))
    if any(it.get("doc_url") for it in items):
        el.append(Paragraph("Invoice numbers are clickable - they open the original document in the CATDAY system.", TINY))
    el.append(Spacer(1, 6 * mm))
    el.append(_sig_block(["Prepared By", "Approved By", "Received By"]))
    el.append(Spacer(1, 4 * mm))
    el.append(Paragraph(f"Generated by CATDAY System - {pv_no}", TINY))
    doc.build(el)
    return rel


def listing_pdf(pl_no: str, vouchers: list[dict], total: float,
                company="CATDAY SDN BHD", address="Uptown PJ") -> str:
    """vouchers: [{pv_no, date, payee, total, bank?}] where bank is a
    'Bank / Account No.' display string (may be empty)."""
    subdir = f"listings/{date.today():%Y-%m}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel = f"{subdir}/{pl_no}_{date.today():%Y-%m-%d}.pdf"
    doc = SimpleDocTemplate(os.path.join(UPLOAD_DIR, rel), pagesize=A4,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    el = []
    _header(el, company, address, "PAYMENT LISTING")
    el.append(Paragraph(f"Listing No: {pl_no}     Date: {date.today():%d/%m/%Y}     "
                        f"Vouchers: {len(vouchers)}", styles["Normal"]))
    el.append(Spacer(1, 5 * mm))

    data = [["No.", "PV No.", "Date", "Payee", "Bank / Account No.", "Amount (RM)"]]
    for i, v in enumerate(vouchers, 1):
        data.append([str(i), v["pv_no"], v["date"], Paragraph(v["payee"], SMALL),
                     Paragraph(v.get("bank") or "-", SMALL), f"{v['total']:,.2f}"])
    data.append(["", "", "", "", "GRAND TOTAL", f"{total:,.2f}"])
    t = Table(data, colWidths=[10 * mm, 21 * mm, 20 * mm, 48 * mm, 45 * mm, 26 * mm], repeatRows=1)
    t.setStyle(_grid_style())
    t.setStyle(TableStyle([("ALIGN", (5, 0), (5, -1), "RIGHT")]))
    el.append(t)
    el.append(Spacer(1, 8 * mm))
    el.append(_sig_block(["Prepared By", "Approved By"]))
    el.append(Spacer(1, 4 * mm))
    el.append(Paragraph(f"Generated by CATDAY System - {pl_no}", TINY))
    doc.build(el)
    return rel


def invoice_pdf(inv_no: str, customer: str, cust_address: str, cust_contact: str,
                items: list[dict], due_date: str, notes: str = "",
                deposit_paid: float = 0.0, schedule: list[dict] | None = None,
                company="MEOW & ME PET SHOP SDN BHD", address="", reg_no="",
                bank: dict | None = None, tagline="a good day for every cat.") -> str:
    """Customer sales invoice in the cat day brand system (Brand Guidelines,
    Aug 2026): Gliker logotype, Onest body text, the five brand colours, and
    the logo reversed out of a dark-brown ground per the primary logo-usage
    rule ("if background is dark in colour, the logo appears in #ECDBB6").

    `company` / `reg_no` carry the LEGAL entity — Malaysian invoices must show
    the registered name and company number even when the trading brand differs.

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
    doc = SimpleDocTemplate(os.path.join(UPLOAD_DIR, rel), pagesize=A4,
                            topMargin=0, bottomMargin=14 * mm,
                            leftMargin=17 * mm, rightMargin=17 * mm)
    el = []

    label = ParagraphStyle("lbl", fontName=BODY_F, fontSize=7.5, textColor=BRAND_TEAL,
                           leading=10.5)
    value = ParagraphStyle("val", fontName=BODY_BOLD_F, fontSize=10.5,
                           textColor=BRAND_BROWN, leading=13.5)
    body = ParagraphStyle("body", fontName=BODY_F, fontSize=9.5,
                          textColor=BRAND_BROWN, leading=13.5)
    onlight = ParagraphStyle("onlight", parent=body, fontSize=9, leading=13)
    cream_head = ParagraphStyle("ch", fontName=BODY_BOLD_F, fontSize=8.5,
                                textColor=BRAND_CREAM, leading=11.5)
    boxtitle = ParagraphStyle("bt", fontName=BODY_BOLD_F, fontSize=7.5,
                              textColor=BRAND_RUST, leading=10)
    foot = ParagraphStyle("foot", fontName=BODY_F, fontSize=7.5,
                          textColor=BRAND_TEAL, leading=11)

    # ── Header band: brand logo reversed out of brand brown ──────────────────
    if os.path.exists(LOGO_CREAM):
        from reportlab.platypus import Image as RLImage
        brandcell = RLImage(LOGO_CREAM, width=44 * mm, height=44 / 2.36 * mm)
        brandcell.hAlign = "LEFT"
    else:
        brandcell = Paragraph("cat day", ParagraphStyle(
            "bn", fontName=DISPLAY_F, fontSize=26, textColor=BRAND_CREAM))
    entity = Paragraph(
        f'<font name="{BODY_BOLD_F}" size=9.5 color="#ECDBB6">{company}</font><br/>'
        + (f'<font size=8 color="#E7CE7A">Company No. {reg_no}</font><br/>' if reg_no else "")
        + (f'<font size=8 color="#ECDBB6">{address}</font>' if address else ""),
        ParagraphStyle("ent", fontName=BODY_F, fontSize=8, leading=12,
                       alignment=2, textColor=BRAND_CREAM))
    head = Table([[brandcell, entity]], colWidths=[64 * mm, 112 * mm])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 17 * mm),
        ("RIGHTPADDING", (1, 0), (1, 0), 17 * mm),
        ("RIGHTPADDING", (0, 0), (0, 0), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 11 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 11 * mm),
    ]))
    band = Table([[head]], colWidths=[210 * mm])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND_BROWN),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    band.hAlign = "CENTER"
    el.append(band)
    el.append(Spacer(1, 9 * mm))

    el.append(Paragraph("SALES INVOICE", ParagraphStyle(
        "title", fontName=DISPLAY_F, fontSize=18, textColor=BRAND_BROWN, leading=21)))
    el.append(Spacer(1, 5 * mm))

    # ── Bill-to / invoice meta ───────────────────────────────────────────────
    addr_bits = "<br/>".join(filter(None, [cust_address,
                                           f"Tel: {cust_contact}" if cust_contact else ""]))
    meta = Table([
        [Paragraph("BILL TO", label), Paragraph("INVOICE NO.", label),
         Paragraph("INVOICE DATE", label), Paragraph("PAYMENT DUE", label)],
        [Paragraph(customer, value), Paragraph(inv_no, value),
         Paragraph(f"{date.today():%d/%m/%Y}", value),
         Paragraph(due_date, ParagraphStyle("due", parent=value, textColor=BRAND_RUST))],
        [Paragraph(addr_bits, onlight), "", "", ""],
    ], colWidths=[78 * mm, 32 * mm, 32 * mm, 34 * mm])
    meta.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("TOPPADDING", (0, 2), (-1, 2), 3),
    ]))
    el.append(meta)
    el.append(Spacer(1, 7 * mm))

    # ── Line items ───────────────────────────────────────────────────────────
    total = sum(it["amount"] for it in items)
    right = lambda s: ParagraphStyle(f"r{id(s)}", parent=s, alignment=2)  # noqa: E731
    data = [[Paragraph("NO.", cream_head), Paragraph("DESCRIPTION", cream_head),
             Paragraph("AMOUNT (RM)", right(cream_head))]]
    for i, it in enumerate(items, 1):
        data.append([Paragraph(str(i), body), Paragraph(it["description"], body),
                     Paragraph(f"{it['amount']:,.2f}", right(body))])
    n_items = len(items)
    tot_style = ParagraphStyle("tot", fontName=BODY_BOLD_F, fontSize=10,
                               textColor=BRAND_BROWN, leading=13)
    data.append(["", Paragraph("TOTAL", tot_style),
                 Paragraph(f"{total:,.2f}", right(tot_style))])
    bal_row = None
    if deposit_paid:
        balance = round(total - deposit_paid, 2)
        data.append(["", Paragraph("Less: Deposit Received", body),
                     Paragraph(f"-{deposit_paid:,.2f}", right(body))])
        bal_style = ParagraphStyle("bal", fontName=BODY_BOLD_F, fontSize=11,
                                   textColor=BRAND_RUST, leading=14)
        data.append(["", Paragraph("BALANCE DUE", bal_style),
                     Paragraph(f"{balance:,.2f}", right(bal_style))])
        bal_row = len(data) - 1
    t = Table(data, colWidths=[12 * mm, 124 * mm, 40 * mm], repeatRows=1)
    tstyle = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_BROWN),
        ("ROWBACKGROUNDS", (0, 1), (-1, n_items), [colors.white, BRAND_CREAM_TINT]),
        ("BACKGROUND", (0, n_items + 1), (-1, n_items + 1), BRAND_CREAM),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]
    if bal_row is not None:
        tstyle += [("BACKGROUND", (0, bal_row), (-1, bal_row), BRAND_CREAM),
                   ("LINEABOVE", (1, bal_row), (-1, bal_row), 1.2, BRAND_RUST)]
    t.setStyle(TableStyle(tstyle))
    el.append(t)
    el.append(Spacer(1, 6 * mm))

    # ── Payment schedule (used when nothing has been received yet) ───────────
    if schedule:
        sh = ParagraphStyle("sh", fontName=BODY_BOLD_F, fontSize=8,
                            textColor=BRAND_BROWN, leading=11)
        sdata = [[Paragraph("PAYMENT SCHEDULE", boxtitle), "", "", ""],
                 [Paragraph("PAYMENT", sh), Paragraph("AMOUNT (RM)", right(sh)),
                  Paragraph("DUE", sh), Paragraph("STATUS", sh)]]
        for s in schedule:
            sdata.append([
                Paragraph(s["label"], onlight),
                Paragraph(f"{s['amount']:,.2f}", right(onlight)),
                Paragraph(s["due"], onlight),
                Paragraph(s.get("status", "Due"),
                          ParagraphStyle("st", parent=onlight, fontName=BODY_BOLD_F,
                                         textColor=BRAND_RUST))])
        st = Table(sdata, colWidths=[44 * mm, 30 * mm, 52 * mm, 50 * mm])
        st.setStyle(TableStyle([
            ("SPAN", (0, 0), (-1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_CREAM),
            ("LINEBELOW", (0, 1), (-1, 1), 0.5, BRAND_CREAM),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOX", (0, 0), (-1, -1), 0.7, BRAND_CREAM),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        el.append(st)
        el.append(Spacer(1, 6 * mm))

    # ── Payment details ──────────────────────────────────────────────────────
    if bank and (bank.get("bank_name") or bank.get("account_no")):
        holder = bank.get("account_holder") or company
        bt = Table([
            [Paragraph("PAYMENT DETAILS", boxtitle), ""],
            [Paragraph("Bank", label), Paragraph(bank.get("bank_name") or "-", value)],
            [Paragraph("Account No.", label), Paragraph(bank.get("account_no") or "-", value)],
            [Paragraph("Account Holder", label), Paragraph(holder, value)],
        ], colWidths=[34 * mm, 142 * mm])
        bt.setStyle(TableStyle([
            ("SPAN", (0, 0), (-1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_CREAM),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.7, BRAND_CREAM),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        el.append(bt)
        el.append(Spacer(1, 5 * mm))

    if notes:
        el.append(Paragraph(
            f'<font name="{BODY_BOLD_F}" color="#B14919">Notes</font><br/>{notes}', onlight))
        el.append(Spacer(1, 9 * mm))

    # Branded signature block — a thin cream rule and an Onest caption, rather
    # than the typewriter underscores the voucher/payslip templates use.
    sig_cap = ParagraphStyle("sig", fontName=BODY_F, fontSize=8,
                             textColor=BRAND_TEAL, leading=11)
    # Middle spacer column keeps the two signature rules visually separate —
    # adjacent LINEBELOWs would run together and read as one long line.
    sig = Table([["", "", ""],
                 [Paragraph("Issued By", sig_cap), "", Paragraph("Received By", sig_cap)]],
                colWidths=[66 * mm, 22 * mm, 66 * mm], hAlign="LEFT")
    sig.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (0, 0), 0.7, BRAND_CREAM),
        ("LINEBELOW", (2, 0), (2, 0), 0.7, BRAND_CREAM),
        ("TOPPADDING", (0, 0), (-1, 0), 20 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 1), (-1, 1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    el.append(sig)
    el.append(Spacer(1, 8 * mm))
    el.append(HRFlowable(width="100%", thickness=0.7, color=BRAND_CREAM))
    el.append(Spacer(1, 2 * mm))
    el.append(Paragraph(
        f'<font name="{DISPLAY_F}" size=9 color="#B14919">{tagline}</font>'
        f'&nbsp;&nbsp;&nbsp;{company}'
        + (f' &middot; Company No. {reg_no}' if reg_no else "")
        + f' &middot; {inv_no}', foot))
    doc.build(el)
    return rel


def payslip_pdf(month: str, item, company="CATDAY SDN BHD", address="Uptown PJ") -> str:
    """item: PayrollItem. Returns relative pdf path."""
    subdir = f"payslips/{safe_name(month)}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel = f"{subdir}/PAYSLIP_{safe_name(month)}_{safe_name(item.staff_name)}.pdf"
    doc = SimpleDocTemplate(os.path.join(UPLOAD_DIR, rel), pagesize=A4,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    el = []
    _header(el, company, address, f"PAYSLIP — {month}")

    info = Table([
        ["Employee:", item.staff_name, "Position:", item.position],
        ["Pay Period:", month, "Payslip Date:", f"{date.today():%d/%m/%Y}"],
    ], colWidths=[28 * mm, 62 * mm, 30 * mm, 50 * mm])
    info.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), GRAY),
        ("TEXTCOLOR", (2, 0), (2, -1), GRAY),
    ]))
    el.append(info)
    el.append(Spacer(1, 6 * mm))

    earnings = [
        ["EARNINGS", "RM"],
        ["Basic Salary", f"{item.base:,.2f}"],
    ]
    if item.allowance:
        earnings.append(["Allowance", f"{item.allowance:,.2f}"])
    if item.overtime:
        earnings.append(["Overtime", f"{item.overtime:,.2f}"])
    if item.commission:
        earnings.append(["Commission", f"{item.commission:,.2f}"])
    if item.bonus:
        earnings.append(["Bonus", f"{item.bonus:,.2f}"])
    if item.leave_deduction:
        earnings.append([f"Less: Unpaid Leave ({item.unpaid_leave_days:g}d)", f"-{item.leave_deduction:,.2f}"])
    earnings.append(["Gross Pay", f"{item.gross:,.2f}"])

    deductions = [
        ["DEDUCTIONS", "RM"],
        ["EPF (Employee)", f"{item.epf_ee:,.2f}"],
        ["SOCSO (Employee)", f"{item.socso_ee:,.2f}"],
        ["EIS (Employee)", f"{item.eis_ee:,.2f}"],
    ]
    if item.pcb:
        deductions.append(["PCB / MTD (Tax)", f"{item.pcb:,.2f}"])
    if item.deductions:
        deductions.append(["Other Deductions", f"{item.deductions:,.2f}"])
    total_ded = item.epf_ee + item.socso_ee + item.eis_ee + item.pcb + item.deductions
    deductions.append(["Total Deductions", f"{total_ded:,.2f}"])

    te = Table(earnings, colWidths=[55 * mm, 28 * mm])
    td = Table(deductions, colWidths=[55 * mm, 28 * mm])
    for t in (te, td):
        t.setStyle(_grid_style())
        t.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT")]))
    pair = Table([[te, td]], colWidths=[88 * mm, 88 * mm])
    pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    el.append(pair)
    el.append(Spacer(1, 6 * mm))

    net = Table([["NET PAY", f"RM {item.net:,.2f}"]], colWidths=[120 * mm, 51 * mm])
    net.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ORANGE),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 12),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    el.append(net)
    el.append(Spacer(1, 5 * mm))

    er = Table([
        ["EMPLOYER CONTRIBUTIONS (not deducted from pay)", "RM"],
        ["EPF (Employer)", f"{item.epf_er:,.2f}"],
        ["SOCSO (Employer)", f"{item.socso_er:,.2f}"],
        ["EIS (Employer)", f"{item.eis_er:,.2f}"],
        ["Total Employer Cost", f"{item.employer_cost:,.2f}"],
    ], colWidths=[120 * mm, 51 * mm])
    er.setStyle(_grid_style())
    er.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT")]))
    el.append(er)

    if item.remarks:
        el.append(Spacer(1, 4 * mm))
        el.append(Paragraph(f"Remarks: {item.remarks}", SMALL))

    el.append(Spacer(1, 8 * mm))
    el.append(_sig_block(["Employer", "Employee"]))
    el.append(Spacer(1, 4 * mm))
    el.append(Paragraph("This is a computer-generated payslip. Generated by CATDAY System.", TINY))
    doc.build(el)
    return rel
