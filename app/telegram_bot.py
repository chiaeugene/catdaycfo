"""Telegram intake: webhook handler + shared processing logic.

The same handle_update() is used by the production webhook (FastAPI route)
and by poll_bot.py for local development.
"""
import json
import os
from datetime import datetime, date

import httpx
from sqlalchemy.orm import Session

from . import claude_ai
from .models import Document, Payment, Setting, User

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")

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


def bot_token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def tg_send(chat_id, text: str):
    token = bot_token()
    if not token:
        return
    httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=30,
    )


def tg_get_file(file_id: str) -> tuple[bytes, str]:
    token = bot_token()
    meta = httpx.get(f"https://api.telegram.org/bot{token}/getFile",
                     params={"file_id": file_id}, timeout=30).json()
    path = meta["result"]["file_path"]
    data = httpx.get(f"https://api.telegram.org/file/bot{token}/{path}", timeout=120).content
    return data, path


def next_counter(db: Session, name: str, prefix: str) -> str:
    from .models import Counter
    c = db.get(Counter, name)
    if not c:
        c = Counter(name=name, value=1)
        db.add(c)
    n = c.value
    c.value = n + 1
    db.flush()
    return f"{prefix}{n:04d}"


def handle_update(update: dict, db: Session):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    frm = msg.get("from", {})
    from_id = str(frm.get("id", ""))
    from_name = " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])) \
        or frm.get("username") or from_id

    # Whitelist: '*' or registered telegram_ids
    wl_setting = db.get(Setting, "TELEGRAM_WHITELIST")
    wl = (wl_setting.value if wl_setting else "*").strip() or "*"
    if wl != "*":
        allowed = {x.strip() for x in wl.split(",")}
        known = {u.telegram_id for u in db.query(User).filter(User.telegram_id != "").all()}
        if from_id not in allowed | known:
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
        text = (msg.get("text") or "").strip()
        if not text or text.startswith("/"):
            tg_send(chat_id, HELP_TEXT)
            return
        handle_text_report(chat_id, from_name, text, db)
        return

    tg_send(chat_id, "📄 Document received, processing...\n文件已收到，处理中...")

    data, _ = tg_get_file(file_id)
    caption = msg.get("caption", "")
    cls = claude_ai.classify(data, mime, caption, filename)

    # Save file
    doc_no = next_counter(db, "DOC", "DOC-")
    subdir = f"{date.today():%Y-%m}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel_path = f"{subdir}/{doc_no}_{filename}"
    with open(os.path.join(UPLOAD_DIR, rel_path), "wb") as f:
        f.write(data)

    month = cls.get("month") or f"{date.today():%b %Y}"
    doc = Document(
        doc_no=doc_no, sender=from_name, section=cls.get("section", "Expense"),
        doc_type=cls.get("doc_type", "Other"),
        supplier=cls.get("supplier", ""), amount=cls.get("amount", 0.0),
        month=month, description=cls.get("description") or caption or filename,
        category=cls.get("category", ""), invoice_no=cls.get("invoice_no", ""),
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
           "ℹ️ Basic classification only.  仅基础分类。"))


def handle_text_report(chat_id, from_name: str, text: str, db: Session):
    """A typed report (no file). Classify → pending Document → await verification."""
    cls = claude_ai.classify_text(text)
    itype = cls.get("intake_type", "Unknown")

    if itype == "Unknown":
        tg_send(chat_id,
            "🤔 I couldn't tell what this is.\n我无法识别这条信息。\n\n"
            "Try one of these formats — or send a photo:\n请用以下格式，或发送照片：\n\n" + HELP_TEXT)
        return

    section_map = {"Sales Report": "Sales Report", "Petty Cash": "Petty Cash",
                   "Staff Claim": "Staff Claim", "Boarding Log": "Boarding Log"}
    section = section_map.get(itype, "Filing Only")
    doc_no = next_counter(db, "DOC", "DOC-")

    payload = {}
    summary_lines = []
    amount = float(cls.get("amount") or 0)

    if itype == "Sales Report":
        sales = [s for s in cls.get("sales", []) if s.get("amount")]
        payload = {"sales": sales}
        amount = sum(float(s["amount"]) for s in sales)
        summary_lines = [f"🛒 {s['stream']}: RM {float(s['amount']):,.2f}" for s in sales]
    elif itype == "Boarding Log":
        b = cls.get("boarding") or {}
        payload = {"boarding": b}
        summary_lines = [f"🏨 Check-in: {b.get('checked_in', 0)}  ·  Check-out: {b.get('checked_out', 0)}"
                         f"  ·  In-house: {b.get('occupancy', 0)}"]
    elif itype in ("Petty Cash", "Staff Claim"):
        summary_lines = [f"💰 Amount: RM {amount:,.2f}"]
        if cls.get("category"):
            summary_lines.append(f"🏷 Category: {cls['category']}")

    doc = Document(
        doc_no=doc_no, sender=from_name, section=section, doc_type="Report",
        intake_type=itype, supplier=cls.get("supplier", ""), amount=amount,
        month=f"{date.today():%b %Y}", description=cls.get("description") or text[:120],
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
           "ℹ️ Basic parsing.  基础识别。"))
