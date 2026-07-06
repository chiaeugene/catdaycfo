"""Document classification — Claude API if key set, heuristic fallback."""
import base64
import json
import os
import re

import httpx

from .models import CATEGORIES, DOC_TYPES, DOC_SECTIONS

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
