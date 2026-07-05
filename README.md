# CATDAY Finance System 🐱

Web-based finance system for CATDAY cat hotel. The old Google Sheet remains as
business reference only — this system is the operational machine.

## Stack
FastAPI + SQLite (SQLAlchemy) + Jinja2 server-rendered UI + Telegram bot +
Claude AI document classification + reportlab PDF vouchers/listings.

## Modules
- **📥 Documents** — intake via Telegram bot or web upload; AI classifies type/supplier/amount
- **💳 Payments** — categorize (16 categories, CAPEX/OPEX/COGS/Payroll groups); invoices auto-create rows
- **🧾 Vouchers** — tick payments → numbered PV PDF with signature blocks
- **📑 Listings** — tick vouchers → PL batch PDF for approval/bank
- **🐷 Petty Cash** — out/in log with running balance
- **🛒 Sales** — daily takings by stream (Boarding/Grooming/Cat Sales/Membership/Retail)
- **💰 Payroll** — staff register + monthly runs
- **📈 P&L** — live monthly P&L from actual data
- **⚙️ Settings** — users (admin/manager/staff roles), company info, bot whitelist

## Run locally
```powershell
python -m pip install -r requirements.txt
copy .env.example .env        # fill in TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY
python seed.py                # creates eugene/karen logins + 9 staff
python -m uvicorn app.main:app --port 8123
# separate terminal, for the bot during local dev:
python poll_bot.py
```
Login: `eugene` / `catday2026` (change after first login).

## Deploy to Railway
1. Push this folder to a GitHub repo (or `railway up` via CLI)
2. Add a **Volume** mounted at `/data`; set env vars:
   - `DATABASE_URL=sqlite:////data/catday.db`
   - `UPLOAD_DIR=/data/uploads`
   - `SECRET_KEY`, `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `WEBHOOK_SECRET`
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Set the Telegram webhook (replace values):
   `https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<app>.railway.app/telegram/webhook/<WEBHOOK_SECRET>`
5. Run `python seed.py` once via Railway shell.

In production the webhook handles the bot — do **not** run poll_bot.py there.

## Roles
| Role | Sees |
|---|---|
| admin | everything (payroll, P&L, settings) |
| manager | + payments, vouchers, listings |
| staff | dashboard, documents, petty cash, sales |
