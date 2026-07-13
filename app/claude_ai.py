"""Document classification — Claude API if key set, heuristic fallback."""
import base64
import json
import os
import re

import httpx

from .models import CATEGORIES, DOC_TYPES, DOC_SECTIONS, STREAMS

PROMPT = (
    "You are a finance document classifier for CATDAY, a premium cat hotel in Malaysia. "
    "Analyze this document and return ONLY a JSON object (no markdown fences) with keys:\n"
    f'"doc_type": one of {DOC_TYPES},\n'
    f'"section": one of {DOC_SECTIONS} — routing rules: large supplier invoices/asset purchases = "Purchase"; '
    'utility bills, subscriptions, service invoices = "Expense"; '
    'a receipt a staff member paid personally and wants reimbursed (caption mentions claim/reimburse/paid myself) = "Staff Claim"; '
    'small cash receipts (shop/petrol/food, typically under RM200 cash) = "Petty Cash"; '
    'bank deposit slips = "Bank-in Slip"; salary documents = "Payroll"; quotations/statements/anything not creating a transaction = "Filing Only",\n'
    '"supplier": company/shop name (for Staff Claim: the claimant staff name from the caption) or "",\n'
    '"invoice_no": the invoice/receipt/reference number printed on the document, or "",\n'
    '"amount": total amount as a number (no currency symbol) or 0,\n'
    '"month": billing month as "MMM yyyy" (e.g. "Jul 2026") or "",\n'
    '"description": one short line describing what this payment is for,\n'
    f'"category": best guess from {CATEGORIES} (use "Staff Claim" for staff reimbursements) or ""\n'
)


def classify(data: bytes, mime: str, caption: str = "", filename: str = "") -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        try:
            return _classify_claude(data, mime, caption, key)
        except Exception:
            pass
    return _classify_heuristic(caption, filename, mime)


def _classify_claude(data: bytes, mime: str, caption: str, key: str) -> dict:
    b64 = base64.b64encode(data).decode()
    if mime == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": mime, "data": b64}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
    text = PROMPT + (f"\nSender's caption: {caption}" if caption else "")
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 500,
            "messages": [{"role": "user", "content": [block, {"type": "text", "text": text}]}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"]
    raw = re.sub(r"```json|```", "", raw).strip()
    out = json.loads(raw)
    out["ai"] = True
    out["amount"] = float(out.get("amount") or 0)
    return out


def _classify_heuristic(caption: str, filename: str, mime: str) -> dict:
    t = f"{caption} {filename}".lower()
    doc_type, section = "Other", "Filing Only"
    if re.search(r"bank[- ]?in|deposit|cdm|存款", t):
        doc_type, section = "Bank-in Slip", "Bank-in Slip"
    elif re.search(r"claim|reimburs|报销", t):
        doc_type, section = "Receipt", "Staff Claim"
    elif re.search(r"invoice|inv[-_ ]?\d|发票", t):
        doc_type, section = "Invoice", "Expense"
    elif re.search(r"petty|cash|零用", t):
        doc_type, section = "Receipt", "Petty Cash"
    elif re.search(r"receipt|resit|收据", t):
        doc_type, section = "Receipt", "Expense"
    elif re.search(r"quot|报价", t):
        doc_type, section = "Quotation", "Filing Only"
    elif re.search(r"statement|对账", t):
        doc_type, section = "Statement", "Filing Only"
    elif re.search(r"payslip|salary|工资", t):
        doc_type, section = "Payslip", "Payroll"
    elif mime.startswith("image"):
        doc_type, section = "Receipt", "Expense"
    m = re.search(r"rm\s*([\d,]+\.?\d*)", t)
    inv = re.search(r"\b(?:inv|invoice|ref)[#:\s-]*([A-Za-z0-9-]{3,20})\b", t)
    return {
        "doc_type": doc_type,
        "section": section,
        "supplier": "",
        "invoice_no": inv.group(1).upper() if inv else "",
        "amount": float(m.group(1).replace(",", "")) if m else 0.0,
        "month": "",
        "description": caption or filename,
        "category": "Staff Claim" if section == "Staff Claim" else "",
        "ai": False,
    }


# ══════════════ TEXT-MESSAGE SUBMISSIONS (reports typed to the bot) ══════════════
TEXT_PROMPT = (
    "You are the intake AI for CATDAY, a premium cat hotel in Malaysia. A staff member "
    "typed a message to the finance/ops bot. Figure out what it is, even if they didn't "
    "follow any template. Return ONLY a JSON object (no markdown) with keys:\n"
    '"intake_type": one of ["Purchase","Expense","Petty Cash","Staff Claim","Sales Report","Boarding Log","Unknown"],\n'
    f'"sales": for a Sales Report, a list of {{"stream": one of {STREAMS}, "amount": number}}, else [],\n'
    '"boarding": for a Boarding Log, {"checked_in": int, "checked_out": int, "occupancy": int}, else null,\n'
    '"amount": the RM amount as a number (for Purchase/Expense/Petty Cash/Staff Claim), else 0,\n'
    f'"category": best guess from {CATEGORIES}, else "",\n'
    '"supplier": the supplier/company name; for Staff Claim the claimant staff name; else "",\n'
    '"invoice_no": the invoice/receipt/reference number if mentioned, else "",\n'
    '"description": one short line summarising the message,\n'
    '"date": the date mentioned as "yyyy-mm-dd" if any, else "".\n'
    "Routing rules:\n"
    "- A supplier/company invoice for goods or services, especially with an invoice number "
    "or a business name (e.g. 'Purchase from Whiskers Wholesale invoice WW-3312 RM1860') "
    "= \"Purchase\" if it's an asset/equipment/renovation, otherwise \"Expense\".\n"
    "- Small out-of-pocket cash spending with NO supplier invoice (e.g. 'bought cat litter RM48') "
    "= \"Petty Cash\".\n"
    "- 'I paid ... please claim/reimburse me' = \"Staff Claim\".\n"
    "- Daily takings / sales / 营业额 = \"Sales Report\".\n"
    "- Cats checked in/out / occupancy / 寄宿 counts = \"Boarding Log\".\n"
    "- Greetings or unclear chatter = \"Unknown\".\n"
    "Key distinction: if a business/supplier name OR an invoice number is present, it is a "
    "Purchase/Expense, NOT Petty Cash — even if the item is small."
)


def classify_text(text: str) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key and len(text.strip()) > 3:
        try:
            return _classify_text_claude(text, key)
        except Exception:
            pass
    return _classify_text_heuristic(text)


def _classify_text_claude(text: str, key: str) -> dict:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 600,
            "messages": [{"role": "user", "content": TEXT_PROMPT + "\n\nMessage:\n" + text}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"]
    raw = re.sub(r"```json|```", "", raw).strip()
    out = json.loads(raw)
    out["ai"] = True
    out["amount"] = float(out.get("amount") or 0)
    out.setdefault("sales", [])
    out.setdefault("boarding", None)
    out.setdefault("supplier", "")
    out.setdefault("invoice_no", "")
    return out


def _classify_text_heuristic(text: str) -> dict:
    t = text.lower()
    out = {"intake_type": "Unknown", "sales": [], "boarding": None, "amount": 0.0,
           "category": "", "supplier": "", "invoice_no": "", "description": text[:80],
           "date": "", "ai": False}

    # Sales report FIRST (the word "boarding" is also a sales stream)
    streams_found = []
    for s in STREAMS:
        mm = re.search(rf"{s.lower()}\D{{0,8}}(?:rm)?\s*([\d,]+\.?\d*)", t)
        if mm:
            streams_found.append({"stream": s, "amount": float(mm.group(1).replace(",", ""))})
    if re.search(r"sales|takings|营业|销售|revenue", t) or len(streams_found) >= 2:
        out["intake_type"] = "Sales Report"
        out["sales"] = streams_found
        return out

    # Boarding log: needs explicit check-in/out or occupancy signals
    if re.search(r"check[- ]?in|check[- ]?out|occupanc|in[- ]?house|入住|退房|现有|头猫|cats?\b", t) and re.search(r"\d", t):
        ci = re.search(r"(?:check[- ]?in|入住)\D{0,6}(\d+)", t)
        co = re.search(r"(?:check[- ]?out|退房)\D{0,6}(\d+)", t)
        occ = re.search(r"(?:occupanc|in[- ]?house|current|now|现有|共|总)\D{0,8}(\d+)", t) \
              or re.search(r"(\d+)\s*cats?\b", t)
        if ci or co or occ:
            out["intake_type"] = "Boarding Log"
            out["boarding"] = {"checked_in": int(ci.group(1)) if ci else 0,
                               "checked_out": int(co.group(1)) if co else 0,
                               "occupancy": int(occ.group(1)) if occ else 0}
            return out

    amt = re.search(r"rm\s*([\d,]+\.?\d*)", t)
    amt_val = float(amt.group(1).replace(",", "")) if amt else 0.0
    # "invoice" before "inv" so the short form doesn't match the prefix of the long one
    inv = re.search(r"\b(?:invoice|inv|ref)\b[#:.\s-]*([a-z0-9][a-z0-9/-]{2,20})", t)
    invoice_no = inv.group(1).upper() if inv else ""
    sup = re.search(r"(?:supplier|from|payee|vendor|供应商)[:\s]+([A-Za-z][\w &.'-]{2,40})", text, re.I)
    supplier = ""
    if sup:
        # stop the name at the next field keyword / delimiter
        supplier = re.split(r"\s*(?:,|\n|invoice|inv\b|amount|rm\s|for:|date:)",
                            sup.group(1), maxsplit=1, flags=re.I)[0].strip()

    if re.search(r"claim|reimburs|报销|paid.*(myself|out of pocket)", t):
        out.update(intake_type="Staff Claim", amount=amt_val, category="Staff Claim")
    # Supplier invoice / purchase (business name or invoice no present) → Expense/Purchase
    elif invoice_no or supplier or re.search(r"purchase|invoice|发票|采购", t):
        is_capex = bool(re.search(r"renovation|equipment|furniture|machine|asset|装修|设备", t))
        out.update(intake_type="Purchase" if is_capex else "Expense",
                   amount=amt_val, supplier=supplier, invoice_no=invoice_no)
    elif re.search(r"petty|cash|bought|beli|买|买了", t) and amt_val:
        out.update(intake_type="Petty Cash", amount=amt_val)
    return out
