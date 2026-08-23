"""Telegram intake: webhook handler + shared processing logic.

The same handle_update() is used by the production webhook (FastAPI route)
and by poll_bot.py for local development.
"""
import json
import os
import re
from datetime import datetime, date

import httpx
from sqlalchemy.orm import Session

from . import claude_ai
from .models import Document, Payment, Setting, User

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
BASE_URL = os.environ.get("BASE_URL", "https://catday-system.onrender.com").rstrip("/")

HELP_TEXT = (
    "🐱 *CATDAY Bot*\n\n"
    "📸 *Send a photo/PDF* of any document — invoice, receipt, bank-in slip — "
    "I'll read it and file it.\n发送发票/收据/银行水单照片，我会自动识别归档。\n\n"
    "⌨️ *Or just type a report* — you don't need a fixed format, I'll understand:\n"
    "也可以直接打字汇报，不必按固定格式：\n\n"
    "🛒 *Daily sales 每日营业额*\n"
    "`Sales today: boarding 440, grooming 300, retail 120`\n\n"
    "🐷 *Petty cash 零用金*\n"
    "`Bought cat litter RM48`\n\n"
    "🧾 *Staff claim 员工报销*\n"
    "`Claim petrol RM68, I paid myself`\n\n"
    "🏨 *Boarding log 寄宿记录*\n"
    "`Check in 3, check out 1, now 22 cats`\n\n"
    "Everything goes to the admin for verification first. ✅\n所有记录先经管理员审核。"
)


# Daily sales report Karen fills in and sends. Categorised by revenue stream
# because that is what the P&L, the sales ledger and the per-service costing all
# key off — a single day total can't be split back out afterwards. Service
# counts feed Stock & Usage (sessions × recipe = consumables used).
DAILY_TEMPLATE = (
    "📋 *CAT DAY — DAILY SALES REPORT*\n"
    "Copy this, fill in the numbers, send it here. Leave a line as `-` if none.\n"
    "复制以下格式，填上数字后发送。没有就填 `-`。\n\n"
    "```\n"
    "CAT DAY DAILY SALES\n"
    "Date: dd/mm/yyyy\n"
    "\n"
    "SALES BY SERVICE\n"
    "Boarding   : RM \n"
    "Grooming   : RM \n"
    "Cat Sales  : RM \n"
    "Membership : RM \n"
    "Retail     : RM \n"
    "Other      : RM \n"
    "Gross Sales: RM \n"
    "SST        : RM \n"
    "Service Chg: RM \n"
    "TOTAL SALES: RM \n"
    "\n"
    "PAYMENT METHOD\n"
    "Cash       : RM \n"
    "Card       : RM \n"
    "DuitNow/QR : RM \n"
    "Bank Xfer  : RM \n"
    "\n"
    "SERVICES DONE\n"
    "Grooming sessions : \n"
    "Boarding nights   : \n"
    "\n"
    "Notes: \n"
    "```\n"
    "💡 Sales *by service* matter most — that is what feeds the P&L and shows "
    "which service actually makes money.\n"
    "按服务分类最重要，这样才能看出哪项服务赚钱。"
)


def bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def tg_send(chat_id, text: str, buttons=None):
    """buttons: list of rows, each a list of {text, url} dicts → inline keyboard."""
    token = bot_token()
    if not token:
        return
    payload = {"chat_id": str(chat_id), "text": text, "parse_mode": "Markdown"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
               json=payload, timeout=30)


def verify_button():
    """Inline button that opens the Verification queue on the web app."""
    return [[{"text": "🔗 Open verification 打开审核", "url": f"{BASE_URL}/documents"}]]


def tg_get_file(file_id: str) -> tuple[bytes, str]:
    token = bot_token()
    meta = httpx.get(f"https://api.telegram.org/bot{token}/getFile",
                     params={"file_id": file_id}, timeout=30).json()
    path = meta["result"]["file_path"]
    data = httpx.get(f"https://api.telegram.org/file/bot{token}/{path}", timeout=120).content
    return data, path


def next_counter(db: Session, name: str, prefix: str) -> str:
    from .models import Counter, Setting
    # Prefix override from Settings, e.g. PREFIX_PV = "CD-PV-"
    ps = db.get(Setting, f"PREFIX_{name}")
    if ps and ps.value.strip():
        prefix = ps.value.strip()
    c = db.get(Counter, name)
    if not c:
        c = Counter(name=name, value=1)
        db.add(c)
    n = c.value
    c.value = n + 1
    db.flush()
    return f"{prefix}{n:04d}"


def next_monthly_counter(db: Session, name: str, prefix: str, d=None) -> str:
    """Reference number that restarts each month: PL-2608-001, PL-2608-002, …
    then PL-2609-001. Requested by Weng Teng for payment listings — the month
    is readable straight off the reference."""
    from datetime import date as _date
    from .models import Counter, Setting
    ps = db.get(Setting, f"PREFIX_{name}")
    if ps and ps.value.strip():
        prefix = ps.value.strip()
    yymm = f"{(d or _date.today()):%y%m}"
    c = db.get(Counter, f"{name}{yymm}")
    if not c:
        c = Counter(name=f"{name}{yymm}", value=1)
        db.add(c)
    n = c.value
    c.value = n + 1
    db.flush()
    return f"{prefix}{yymm}-{n:03d}"


# Signals that a typed group message is a report rather than chat. Used as a
# cheap local gate BEFORE spending an AI call: in a group the bot sees every
# message, so classifying all of them would be slow and costly.
REPORT_HINTS = re.compile(
    r"\b(?:rm|myr)\s*[\d,]|"
    r"\b(?:sales|takings|revenue|total|gross|nett?|deposit|top\s*up|boarding|"
    r"grooming|check[\s-]?in|check[\s-]?out|occupanc|in[\s-]?house|petty|claim|"
    r"reimburse|invoice|receipt|payment|paid|expense|purchase|sst|service\s*charge)\b|"
    r"(?:营业|销售|收入|总额|寄宿|美容|"
    r"入住|退房|报销|发票|收据|付款|"
    r"零用|采购)",
    re.I)


def looks_like_report(text: str) -> bool:
    """True if a group message is worth classifying. Needs a number plus at
    least one finance/ops signal — 'ok', 'thanks', 'on the way' never match."""
    if len(text) < 12 or not re.search(r"\d", text):
        return False
    return bool(REPORT_HINTS.search(text))


def handle_update(update: dict, db: Session):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    chat_type = msg.get("chat", {}).get("type", "private")
    is_group = chat_type in ("group", "supergroup")
    frm = msg.get("from", {})
    from_id = str(frm.get("id", ""))
    from_name = " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])) \
        or frm.get("username") or from_id

    # In a group the bot sees every message (privacy mode off). It acts on
    # files, on anything addressed to it, and on typed text that looks like a
    # report; everything else is ignored so it never spams the chat.
    text = (msg.get("text") or msg.get("caption") or "").strip()
    bot_un = "@catdaycfobot"
    mentioned = bot_un.lower() in text.lower()
    replied_to_bot = bool(msg.get("reply_to_message", {}).get("from", {}).get("is_bot"))
    # strip the @mention so the AI classifies the actual content
    if mentioned:
        text = text.replace(bot_un, "").replace(bot_un.lower(), "").strip()

    # Whitelist: '*' or registered telegram_ids
    wl_setting = db.get(Setting, "TELEGRAM_WHITELIST")
    wl = (wl_setting.value if wl_setting else "*").strip() or "*"
    if wl != "*":
        allowed = {x.strip() for x in wl.split(",")}
        known = {u.telegram_id for u in db.query(User).filter(User.telegram_id != "").all()}
        if from_id not in allowed | known:
            if not is_group:   # never scold people inside a group
                tg_send(chat_id, f"⛔ Not authorized. 无权限。\nYour Telegram ID: `{from_id}`\n(Ask admin to add you.)")
            return

    # Extract file
    file_id, filename, mime = None, None, None
    if msg.get("photo"):
        file_id = msg["photo"][-1]["file_id"]
        filename = f"photo_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        mime = "image/jpeg"
    elif msg.get("document"):
        d = msg["document"]
        file_id = d["file_id"]
        filename = d.get("file_name") or f"document_{datetime.now():%Y%m%d_%H%M%S}"
        mime = d.get("mime_type") or "application/octet-stream"

    # No file → treat as a typed report (or a command / greeting)
    if not file_id:
        if text.lower().startswith(("/template", "/report", "/daily")):
            tg_send(chat_id, DAILY_TEMPLATE)   # answer this one even in a group
            return
        if not text or text.startswith("/"):
            if not is_group:            # greetings/commands: reply only in private
                tg_send(chat_id, HELP_TEXT)
            return
        # In a group, reports are captured WITHOUT an @mention — that was the
        # stated requirement ("work without the @mention, just no improper
        # conversation"). Ordinary chatter is filtered out twice: cheaply by
        # looks_like_report() before any AI call, then by handle_text_report,
        # which stays silent when the classifier returns "Unknown".
        if is_group and not (mentioned or replied_to_bot) and not looks_like_report(text):
            return
        handle_text_report(chat_id, from_name, text, db, silent_if_unknown=is_group)
        return

    tg_send(chat_id, "📄 Document received, processing...\n文件已收到，处理中...")

    data, _ = tg_get_file(file_id)
    caption = msg.get("caption", "")
    from .models import Supplier
    known_suppliers = [s.name for s in db.query(Supplier)
                       .filter(Supplier.active == True).all()]  # noqa: E712
    cls = claude_ai.classify(data, mime, caption, filename, supplier_names=known_suppliers)

    # Save file
    doc_no = next_counter(db, "DOC", "DOC-")
    subdir = f"{date.today():%Y-%m}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel_path = f"{subdir}/{doc_no}_{filename}"
    with open(os.path.join(UPLOAD_DIR, rel_path), "wb") as f:
        f.write(data)

    month = cls.get("month") or f"{date.today():%b %Y}"
    doc_date = None
    if cls.get("date"):
        try:
            doc_date = datetime.strptime(cls["date"], "%Y-%m-%d").date()
        except ValueError:
            pass
    doc = Document(
        doc_no=doc_no, sender=from_name, section=cls.get("section", "Expense"),
        doc_type=cls.get("doc_type", "Other"),
        supplier=cls.get("supplier", ""), amount=cls.get("amount", 0.0),
        month=month, description=cls.get("description") or caption or filename,
        category=cls.get("category", ""), invoice_no=cls.get("invoice_no", ""),
        doc_date=doc_date,
        payload_json=json.dumps({"tax_type": cls.get("tax_type", "None")}),
        file_path=rel_path, mime=mime, ai_classified=cls.get("ai", False),
        status="Pending",
    )
    db.add(doc)
    db.commit()

    tg_send(chat_id,
        "✅ *Received!  已收到！*\n\n"
        f"📋 ID: `{doc_no}`\n"
        f"📁 Type 类型: {doc.doc_type}\n"
        f"📂 Section 分区: {doc.section}\n"
        + (f"🏪 Supplier 供应商: {doc.supplier}\n" if doc.supplier else "")
        + (f"💰 Amount 金额: RM {doc.amount:,.2f}\n" if doc.amount else "")
        + f"🗓 Month 月份: {month}\n\n"
        "🕐 *Awaiting verification 等待审核* — an admin will verify and post it to the system.\n"
        + ("🤖 Pre-filled by AI.  AI 已预填资料。" if cls.get("ai") else
           "ℹ️ Basic classification only.  仅基础分类。"),
        buttons=verify_button())


def handle_text_report(chat_id, from_name: str, text: str, db: Session,
                       silent_if_unknown: bool = False):
    """A typed report (no file). Classify → pending Document → await verification."""
    cls = claude_ai.classify_text(text)
    itype = cls.get("intake_type", "Unknown")

    if itype == "Unknown":
        if not silent_if_unknown:   # in a group, don't reply to chatter
            tg_send(chat_id,
                "🤔 I couldn't tell what this is.\n我无法识别这条信息。\n\n"
                "Try one of these formats — or send a photo:\n请用以下格式，或发送照片：\n\n" + HELP_TEXT)
        return

    # intake_type doubles as the routing section for most types
    section = {"Sales Report": "Sales Report", "Petty Cash": "Petty Cash",
               "Staff Claim": "Staff Claim", "Boarding Log": "Boarding Log",
               "Purchase": "Purchase", "Expense": "Expense"}.get(itype, "Filing Only")
    doc_no = next_counter(db, "DOC", "DOC-")
    # The report states the day it covers ("Date: 20/08/2026"). Without this the
    # verify screen defaults to today and a report sent the next morning posts
    # against the wrong day.
    report_date = None
    if cls.get("date"):
        try:
            report_date = datetime.strptime(cls["date"], "%Y-%m-%d").date()
        except ValueError:
            report_date = None

    payload = {}
    summary_lines = []
    amount = float(cls.get("amount") or 0)
    invoice_no = cls.get("invoice_no", "")

    if itype == "Sales Report":
        sales = [s for s in cls.get("sales", []) if s.get("amount")]
        sst = float(cls.get("sst") or 0)
        service_charge = float(cls.get("service_charge") or 0)
        gross_sales = float(cls.get("gross_sales") or 0)
        payment_breakdown = {k: float(v) for k, v in (cls.get("payment_breakdown") or {}).items() if v}
        # Reference only — not posted to the ledger. Kept alongside the sales
        # lines so whoever verifies can cross-check against the bank deposit
        # without the original WhatsApp-style report being lost.
        total_sales = float(cls.get("total_sales") or 0)
        # A daily report also carries service counts, the cats in/out figures and
        # any deposit taken. Keep them all: they drive Stock & Usage, the boarding
        # log and the deposit follow-up, and re-typing them later is error-prone.
        services = {k: float(v) for k, v in (cls.get("services") or {}).items() if v}
        cats = cls.get("boarding") or None
        deposit = float(cls.get("deposit_received") or 0)
        payload = {"sales": sales, "sst": sst, "service_charge": service_charge,
                   "gross_sales": gross_sales, "total_sales": total_sales,
                   "payment_breakdown": payment_breakdown, "services": services,
                   "boarding": cats, "deposit_received": deposit}
        # Prefer the per-stream breakdown; fall back to the reported totals so a
        # report without service categories still records a real figure instead
        # of RM 0.00. The verifier splits it across streams on the verify card.
        amount = (sum(float(s["amount"]) for s in sales)
                  or total_sales or gross_sales
                  or sum(payment_breakdown.values()))
        summary_lines = [f"🛒 {s['stream']}: RM {float(s['amount']):,.2f}" for s in sales]
        if not sales and amount:
            summary_lines.append(f"💰 Total: RM {amount:,.2f}")
            summary_lines.append("⚠️ No service breakdown — split it by service when verifying")
        if sst:
            summary_lines.append(f"🧾 SST: RM {sst:,.2f}")
        if service_charge:
            summary_lines.append(f"🧾 Service Charge: RM {service_charge:,.2f}")
        if payment_breakdown:
            summary_lines.append("💳 " + " · ".join(
                f"{m}: RM {a:,.2f}" for m, a in payment_breakdown.items()))
        if services:
            summary_lines.append("✂️ " + " · ".join(
                f"{k}: {v:g}" for k, v in services.items()))
        if cats and any(cats.get(k) for k in ("checked_in", "checked_out", "occupancy")):
            summary_lines.append(
                f"🏨 In {cats.get('checked_in', 0)} · Out {cats.get('checked_out', 0)}"
                f" · In-house {cats.get('occupancy', 0)}")
        if deposit:
            summary_lines.append(f"💰 Deposit received: RM {deposit:,.2f}")
    elif itype == "Boarding Log":
        b = cls.get("boarding") or {}
        payload = {"boarding": b}
        summary_lines = [f"🏨 Check-in: {b.get('checked_in', 0)}  ·  Check-out: {b.get('checked_out', 0)}"
                         f"  ·  In-house: {b.get('occupancy', 0)}"]
    elif itype in ("Purchase", "Expense"):
        if cls.get("supplier"):
            summary_lines.append(f"🏪 Supplier 供应商: {cls['supplier']}")
        if invoice_no:
            summary_lines.append(f"🧾 Invoice 发票号: {invoice_no}")
        summary_lines.append(f"💰 Amount 金额: RM {amount:,.2f}")
        if cls.get("category"):
            summary_lines.append(f"🏷 Category 类别: {cls['category']}")
    elif itype in ("Petty Cash", "Staff Claim"):
        summary_lines = [f"💰 Amount: RM {amount:,.2f}"]
        if cls.get("category"):
            summary_lines.append(f"🏷 Category: {cls['category']}")

    doc = Document(
        doc_no=doc_no, sender=from_name, section=section,
        doc_type="Invoice" if itype in ("Purchase", "Expense") else "Report",
        intake_type=itype, supplier=cls.get("supplier", ""), amount=amount,
        invoice_no=invoice_no,
        month=f"{date.today():%b %Y}", description=cls.get("description") or text[:120],
        doc_date=report_date,
        category=cls.get("category", ""), payload_json=json.dumps(payload),
        raw_text=text, ai_classified=cls.get("ai", False), status="Pending",
    )
    db.add(doc)
    db.commit()

    body = "\n".join(summary_lines)
    tg_send(chat_id,
        f"✅ *Got it — {itype}!  已收到！*\n\n"
        f"📋 ID: `{doc_no}`\n"
        + (body + "\n" if body else "")
        + "\n🕐 *Awaiting verification 等待审核* — admin will confirm before it enters the system.\n"
        + ("🤖 Understood by AI.  AI 已识别。" if cls.get("ai") else
           "ℹ️ Basic parsing.  基础识别。"),
        buttons=verify_button())
