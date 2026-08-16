"""Database backups.

Why this exists: the whole business sits in one SQLite file on one Render disk.
Karen's reconstruction, the opening balances, every voucher and payslip — one
disk failure, one bad migration, or one mistaken wipe and it's gone with no way
back. This module gives two independent layers:

  1. Rotating on-disk snapshots (automatic) — protects against bad deploys,
     broken migrations and accidental data loss. Cheap and small, so we keep
     many. Does NOT protect against the disk itself dying.
  2. Downloadable archives (manual) — a full zip of the database plus every
     uploaded document, so a copy can be kept off Render entirely. This is the
     layer that actually survives losing the server, and it needs a human to
     download it somewhere safe.

Snapshots use SQLite's online backup API rather than copying the file. A plain
file copy taken while a write is in flight can produce a subtly corrupt
database that only fails later; the backup API takes a consistent snapshot
under concurrent writes (verified before this was written).
"""
import os
import sqlite3
import zipfile
from datetime import datetime, timezone

from .database import DB_PATH

BACKUP_DIR = os.environ.get("BACKUP_DIR") or (
    "/data/backups" if DB_PATH.startswith("sqlite:////data") else "./backups")
KEEP_SNAPSHOTS = int(os.environ.get("BACKUP_KEEP", "20"))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")


def sqlite_file() -> str | None:
    """Filesystem path of the SQLite DB, or None if we're not on SQLite."""
    if not DB_PATH.startswith("sqlite:"):
        return None
    path = DB_PATH.split("sqlite:///")[-1]
    return path or None


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def make_snapshot(reason: str = "manual") -> dict | None:
    """Take a consistent snapshot of the database. Returns info, or None if
    there's nothing to back up (no DB file yet, or not running on SQLite)."""
    src = sqlite_file()
    if not src or not os.path.exists(src):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_reason = "".join(ch for ch in reason if ch.isalnum() or ch in "-_")[:20] or "manual"
    name = f"catday_{stamp}_{safe_reason}.db"
    dest = os.path.join(BACKUP_DIR, name)

    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)          # consistent even under concurrent writes
        finally:
            target.close()
    finally:
        source.close()

    prune_snapshots()
    return {"name": name, "path": dest, "size": os.path.getsize(dest)}


def list_snapshots() -> list[dict]:
    """Newest first."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for f in os.listdir(BACKUP_DIR):
        if not f.endswith(".db"):
            continue
        p = os.path.join(BACKUP_DIR, f)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": f, "size": st.st_size, "size_h": _fmt_size(st.st_size),
                    "when": datetime.fromtimestamp(st.st_mtime)})
    out.sort(key=lambda x: x["when"], reverse=True)
    return out


def prune_snapshots(keep: int = KEEP_SNAPSHOTS) -> int:
    """Delete the oldest snapshots beyond `keep`. Returns how many were removed."""
    snaps = list_snapshots()
    removed = 0
    for s in snaps[keep:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, s["name"]))
            removed += 1
        except OSError:
            pass
    return removed


def snapshot_path(name: str) -> str | None:
    """Resolve a snapshot filename to a path, refusing anything that tries to
    escape the backup directory."""
    if not name.endswith(".db") or "/" in name or "\\" in name or ".." in name:
        return None
    p = os.path.join(BACKUP_DIR, name)
    if os.path.isfile(p) and os.path.dirname(os.path.abspath(p)) == os.path.abspath(BACKUP_DIR):
        return p
    return None


def build_full_archive(dest_path: str) -> dict:
    """Zip a fresh DB snapshot together with every uploaded document — the
    copy that should be kept off Render. Returns counts for reporting."""
    snap = make_snapshot("download")
    files = 0
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as z:
        if snap:
            z.write(snap["path"], arcname="database/catday.db")
            files += 1
        if os.path.isdir(UPLOAD_DIR):
            for root, _dirs, names in os.walk(UPLOAD_DIR):
                for n in names:
                    full = os.path.join(root, n)
                    rel = os.path.relpath(full, UPLOAD_DIR)
                    try:
                        z.write(full, arcname=os.path.join("uploads", rel))
                        files += 1
                    except OSError:
                        pass
        z.writestr("README.txt",
                   "CATDAY system backup\n"
                   f"Created (UTC): {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}\n\n"
                   "database/catday.db  - full SQLite database (accounts, ledger, payroll,\n"
                   "                      suppliers, audit log, opening balances)\n"
                   "uploads/            - every scanned document, voucher and payslip PDF\n\n"
                   "To restore: stop the app, put catday.db back at the DATABASE_URL path\n"
                   "and the uploads folder back at UPLOAD_DIR, then start the app.\n"
                   "Keep this file somewhere that is NOT the Render disk.\n")
    return {"files": files, "size": os.path.getsize(dest_path)}
