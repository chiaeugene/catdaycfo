"""Local development bot runner (long polling, no webhook needed).

In production (Railway), the webhook route in app/main.py handles updates and
this script is NOT used. Run locally with:  python poll_bot.py
"""
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

from app.database import Base, engine, SessionLocal
from app.telegram_bot import handle_update, bot_token

Base.metadata.create_all(engine)

token = bot_token()
if not token:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first")

# Remove webhook so polling works
httpx.get(f"https://api.telegram.org/bot{token}/deleteWebhook", timeout=30)
print("Polling for Telegram updates... (Ctrl+C to stop)")

offset = 0
while True:
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",
                      params={"offset": offset, "timeout": 50}, timeout=60).json()
        for update in r.get("result", []):
            offset = update["update_id"] + 1
            db = SessionLocal()
            try:
                handle_update(update, db)
            except Exception as e:
                print("Error:", e)
            finally:
                db.close()
    except KeyboardInterrupt:
        break
    except Exception as e:
        print("Poll error:", e)
        time.sleep(5)
