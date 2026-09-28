"""Attendance: the rules and arithmetic, kept out of the routes.

How a clock-in is trusted, in layers:
  1. The QR on the counter changes every 30 seconds (HMAC of the time window
     with the kiosk's secret). A photo of it is useless within minutes.
  2. The kiosk screen only shows a code on the device Karen authorised, so
     opening the page at home shows nothing to scan.
  3. The phone's location is compared with the kiosk's (captured at set-up).
  4. A selfie is taken on every clock-in and clock-out.
  5. A phone is linked to one person, and a new phone waits for Karen.
None of these is unbreakable alone; together they make cheating more effort
than turning up. Anything unusual is flagged, never silently accepted.
"""
import hashlib
import hmac
import math
import os
import secrets
import time
from datetime import date, datetime, timedelta

from . import models as M

MYT = timedelta(hours=8)          # Malaysia has no daylight saving
WINDOW_S = 30                     # the QR changes this often
SCAN_WINDOWS = 4                  # a scanned code is accepted this many windows back (~2 min)
PUNCH_WINDOWS = 12                # ...and the punch must follow within ~6 min (selfie time)
RADIUS_M = 150
SELFIE_KEEP_DAYS = 60
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")


# ─────────────────────────── time ───────────────────────────
def myt(dt: datetime | None) -> datetime | None:
    return dt + MYT if dt else None


def today_myt() -> date:
    return (datetime.utcnow() + MYT).date()


def day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day) - MYT
    return start, start + timedelta(days=1)


# ─────────────────────────── the rotating code ───────────────────────────
def _window(t: float | None = None) -> int:
    return int((t or time.time()) // WINDOW_S)


def code_for(kiosk: M.AttendanceKiosk, window: int | None = None) -> str:
    w = _window() if window is None else window
    return hmac.new(kiosk.secret.encode(), f"{kiosk.id}:{w}".encode(),
                    hashlib.sha256).hexdigest()[:12]


def code_valid(kiosk: M.AttendanceKiosk, code: str, windows_back: int) -> bool:
    now = _window()
    return any(hmac.compare_digest(code_for(kiosk, now - k), code or "")
               for k in range(windows_back + 1))


def seconds_left() -> int:
    return WINDOW_S - int(time.time()) % WINDOW_S


def qr_svg(text: str, size: int = 320) -> str:
    """QR as SVG using reportlab (already a dependency — no new package)."""
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics import renderSVG
    w = QrCodeWidget(text, barLevel="M")
    x0, y0, x1, y1 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    d.add(w)
    return renderSVG.drawToString(d)


def new_kiosk_secret() -> tuple[str, str]:
    return secrets.token_urlsafe(24), secrets.token_hex(16)


def new_device_token() -> str:
    return secrets.token_urlsafe(24)


# ─────────────────────────── place ───────────────────────────
def distance_m(lat1, lng1, lat2, lng2) -> float | None:
    if None in (lat1, lng1, lat2, lng2):
        return None
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


# ─────────────────────────── PIN attempts ───────────────────────────
# In memory: a restart forgives, which is fine for a 4-digit PIN guarded by a
# 30-second QR and a phone that has to be confirmed anyway.
_FAILS: dict[int, list[float]] = {}


def pin_locked(person_id: int) -> bool:
    now = time.time()
    _FAILS[person_id] = [t for t in _FAILS.get(person_id, []) if now - t < 900]
    return len(_FAILS[person_id]) >= 5


def pin_failed(person_id: int) -> None:
    _FAILS.setdefault(person_id, []).append(time.time())


# ─────────────────────────── selfies ───────────────────────────
def save_selfie(person_id: int, data: bytes, ext: str = ".jpg") -> str:
    now = datetime.utcnow() + MYT
    subdir = f"attendance/{now:%Y-%m}"
    os.makedirs(os.path.join(UPLOAD_DIR, subdir), exist_ok=True)
    rel = f"{subdir}/p{person_id}_{now:%d_%H%M%S}_{secrets.token_hex(3)}{ext}"
    with open(os.path.join(UPLOAD_DIR, rel), "wb") as fh:
        fh.write(data)
    return rel


def purge_old_selfies(db) -> int:
    """Selfies are there to check who turned up, not to keep. Delete them after
    SELFIE_KEEP_DAYS; the clock-in record itself stays."""
    key = "ATT_SELFIE_PURGED"
    s = db.get(M.Setting, key)
    today = today_myt().isoformat()
    if s and s.value == today:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=SELFIE_KEEP_DAYS)
    n = 0
    for log in db.query(M.AttendanceLog).filter(M.AttendanceLog.at < cutoff,
                                                M.AttendanceLog.selfie_path != "").all():
        try:
            os.remove(os.path.join(UPLOAD_DIR, log.selfie_path))
        except OSError:
            pass
        log.selfie_path = ""
        n += 1
    if s:
        s.value = today
    else:
        db.add(M.Setting(key=key, value=today))
    db.commit()
    return n


# ─────────────────────────── a person's day ───────────────────────────
def day_summary(logs: list, is_today: bool) -> dict:
    """Pair clock-ins with the clock-out that follows. Rejected scans don't
    count. An 'in' with no 'out' is still-in today, a missing clock-out after."""
    live = sorted([l for l in logs if l.status != "rejected"], key=lambda l: l.at)
    minutes, open_in, first_in, last_out, issues = 0.0, None, None, None, []
    for l in live:
        if l.kind == "in":
            if open_in is not None:
                issues.append("two clock-ins in a row")
            open_in = l
            first_in = first_in or l
        else:
            if open_in is None:
                issues.append("clock-out without clock-in")
            else:
                minutes += (l.at - open_in.at).total_seconds() / 60
                open_in = None
            last_out = l
    state = "in" if open_in is not None else ("out" if live else "absent")
    if open_in is not None and not is_today:
        issues.append("no clock-out")
    if open_in is not None and is_today:
        minutes += (datetime.utcnow() - open_in.at).total_seconds() / 60
    flags = sorted({f for l in live for f in (l.flags or "").split(",") if f})
    return {"state": state, "hours": round(minutes / 60, 2), "first_in": first_in,
            "last_out": last_out, "issues": issues, "flags": flags,
            "pending": any(l.status == "pending" for l in live), "logs": sorted(logs, key=lambda l: l.at)}


FLAG_TEXT = {
    "outside": "scanned away from the shop",
    "no-location": "location not shared",
    "new-phone": "new phone, waiting for Karen",
    "manual": "added by admin",
}
