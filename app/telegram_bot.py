"""Telegram intake: webhook handler + shared processing logic.

The same handle_update() is used by the production webhook (FastAPI route)
and by poll_bot.py for local development.
"""
import os
from datetime import datetime, date

import httpx
from sqlalchemy.orm import Session

from . import claude_ai
from .models import Document, Payment, Setting, User

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")

HELP_TEXT = (
    "🐱 *CATDAY Finance Bot*\n\n"
    "Send me a photo or PDF of any document:\n发送文件照片或PDF给我：\n"
    "• Invoice 发票\n• Receipt 收据\n• Quotation 报价单\n• Statement 对账单\n\n"
    "I will file it, classify it, and log it in the system automatically.\n"
    "我会自动归档、分类并记录到系统。\n\n"
    "💡 Tip: add a caption describing the payment for better accuracy.\n"
    "提示：附上说明文字可提高识别准确度。"
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

    if not file_id:
        tg_send(chat_id, HELP_TEXT)
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
