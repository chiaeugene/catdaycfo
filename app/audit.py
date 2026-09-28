"""Access control + audit trail, enforced in one place.

Why middleware and not per-route checks: several mutating routes in main.py
(e.g. /payments/new, /vouchers/create, /sales/new, /documents/upload) only
ever checked "is someone logged in", not their role — there was no role that
needed excluding before "viewer" existed. Adding a check to every route
individually is exactly the kind of thing that's easy to miss on the next new
route. This middleware makes "viewer can never mutate anything" true for the
whole app, including routes that don't know "viewer" exists, and logs every
mutating request — allowed or blocked — so there's always a trace of who did
what, matching CATDAY's access-control requirement.
"""
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

from .database import SessionLocal
from . import models as M

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# A requester raises purchase requests and sees nothing else. Enforced here,
# not per-route, for the same reason viewer is: a route added next month that
# only checks "logged in?" must still be closed to them.
REQUESTER_PREFIXES = ("/requests", "/static", "/login", "/logout", "/files/requests/", "/health")
# The one mutation a viewer may make: deciding a purchase request, and only
# when can_approve is set. Jasmine changes nothing in the books; this isn't
# the books.
APPROVER_PATH = re.compile(r"^/requests/\d+/decide$")
# The storekeeper (Karen) issues customer invoices, raises purchase requests and
# looks after stock. She never records money received -- payments are recorded
# by the bookkeeper only after they show up in the bank -- and she can't see
# the books. Files are limited to invoices and request photos.
STOREKEEPER_PREFIXES = ("/receivables", "/requests", "/stock", "/attendance", "/files/invoices/",
                        "/files/requests/", "/files/attendance/", "/static", "/login", "/logout",
                        "/health")
# Staff clock in from their own phones with no login, so these must work for
# anyone -- including someone who happens to be signed in as a confined role.
PUBLIC_PREFIXES = ("/attendance/kiosk", "/attendance/scan", "/attendance/punch", "/approve/")
STOREKEEPER_DENY = re.compile(r"^/receivables/(\d+/(receipt|void)|receipt/\d+/delete)$")
# role -> (paths it may reach, where it lands when it strays)
CONFINED_ROLES = {"requester": (REQUESTER_PREFIXES, "/requests"),
                  "storekeeper": (STOREKEEPER_PREFIXES, "/receivables")}
# Never block or log these — the Telegram bot has no user session, and
# blocking /login itself would make it impossible to sign in.
BLOCK_EXEMPT_PREFIXES = ("/telegram/webhook", "/login", "/logout")
NEVER_LOG_PREFIXES = ("/telegram/webhook",)

# Best-effort human labels for the audit log. Falls back to "METHOD /path"
# for anything not listed here — every action still gets logged either way.
ACTION_PATTERNS = [
    (r"^POST /documents/upload$", "Uploaded document"),
    (r"^POST /documents/\d+/verify$", "Verified document"),
    (r"^POST /documents/\d+/reject$", "Rejected document"),
    (r"^POST /documents/\d+/reopen$", "Reopened document for re-verification"),
    (r"^POST /payments/new$", "Added payment"),
    (r"^POST /payments/\d+/update$", "Updated payment"),
    (r"^POST /suppliers/new$", "Added supplier"),
    (r"^POST /suppliers/\d+/update$", "Updated supplier"),
    (r"^POST /suppliers/\d+/tin$", "Updated supplier TIN/BRN"),
    (r"^POST /vouchers/create$", "Created voucher"),
    (r"^POST /vouchers/\d+/action$", "Voucher action"),
    (r"^POST /listings/create$", "Created listing"),
    (r"^POST /listings/\d+/action$", "Listing action"),
    (r"^POST /listings/\d+/add$", "Added vouchers to listing"),
    (r"^POST /listings/\d+/remove/\d+$", "Removed voucher from listing"),
    (r"^POST /listings/\d+/share$", "Issued approver link"),
    (r"^POST /listings/\d+/share/revoke$", "Revoked approver link"),
    (r"^POST /requests/new$", "Created purchase request"),
    (r"^POST /requests/\d+/update$", "Edited purchase request"),
    (r"^POST /requests/\d+/submit$", "Submitted purchase request"),
    (r"^POST /requests/\d+/decide$", "Decided purchase request"),
    (r"^POST /requests/\d+/withdraw$", "Withdrew purchase request"),
    (r"^POST /requests/\d+/cancel$", "Cancelled approved request"),
    (r"^POST /requests/\d+/reopen$", "Reopened purchase request"),
    (r"^POST /requests/\d+/fulfilled$", "Marked purchase request fulfilled"),
    (r"^POST /requests/\d+/delete$", "Deleted draft purchase request"),
    (r"^POST /payments/\d+/link-request$", "Linked payment to purchase request"),
    (r"^POST /settings/users/\d+/approver$", "Toggled approver rights"),
    (r"^POST /listings/\d+/add$", "Added vouchers to a listing"),
    (r"^POST /listings/\d+/remove/\d+$", "Removed a voucher from a listing"),
    (r"^POST /listings/\d+/share$", "Issued an approver link"),
    (r"^POST /listings/\d+/share/revoke$", "Revoked an approver link"),
    (r"^POST /vouchers/\d+/unpay$", "Reversed a paid voucher"),
    (r"^POST /listings/\d+/revert$", "Reverted a processed listing"),
    (r"^POST /sales/\d+/delete$", "Deleted a sales entry"),
    (r"^POST /pettycash/\d+/delete$", "Deleted a petty cash entry"),
    (r"^POST /boarding/\d+/delete$", "Deleted a boarding log"),
    (r"^POST /receivables/receipt/\d+/delete$", "Reversed a customer receipt"),
    (r"^POST /stock/move/\d+/delete$", "Deleted a stock movement"),
    (r"^POST /pettycash/account/new$", "Added petty cash account"),
    (r"^POST /pettycash/new$", "Petty cash entry"),
    (r"^POST /sales/new$", "Recorded sale"),
    (r"^POST /boarding/new$", "Recorded boarding log"),
    (r"^POST /payroll/staff/new$", "Added staff"),
    (r"^POST /payroll/staff/\d+/update$", "Updated staff"),
    (r"^POST /payroll/run$", "Created payroll run"),
    (r"^POST /payroll/run/\d+/item/\d+/update$", "Updated payroll item"),
    (r"^POST /payroll/run/\d+/reopen$", "Reopened payroll run"),
    (r"^POST /payroll/run/\d+/confirm$", "Confirmed payroll run"),
    (r"^POST /payroll/run/\d+/delete$", "Deleted payroll run"),
    (r"^POST /stock/item/new$", "Added stock item"),
    (r"^POST /stock/item/\d+/update$", "Updated stock item"),
    (r"^POST /stock/move$", "Stock movement"),
    (r"^POST /stock/recipe/new$", "Added service recipe"),
    (r"^POST /stock/recipe/\d+/delete$", "Deleted service recipe"),
    (r"^POST /receivables/new$", "Created customer invoice"),
    (r"^POST /attendance/punch$", "Staff clock-in/out"),
    (r"^POST /attendance/kiosk/setup$", "Set up attendance kiosk"),
    (r"^POST /attendance/people/new$", "Added staff to attendance"),
    (r"^POST /attendance/people/\d+/toggle$", "Toggled staff attendance access"),
    (r"^POST /attendance/people/\d+/reset-pin$", "Reset staff attendance PIN"),
    (r"^POST /attendance/device/\d+/confirm$", "Confirmed staff phone"),
    (r"^POST /attendance/device/\d+/reject$", "Rejected staff phone"),
    (r"^POST /attendance/device/\d+/remove$", "Removed staff phone"),
    (r"^POST /attendance/remark$", "Added attendance remark"),
    (r"^POST /attendance/manual$", "Added manual attendance entry"),
    (r"^POST /attendance/log/\d+/void$", "Voided attendance scan"),
    (r"^POST /receivables/\d+/update$", "Edited customer invoice"),
    (r"^POST /receivables/\d+/receipt$", "Recorded customer receipt"),
    (r"^POST /receivables/\d+/void$", "Voided customer invoice"),
    (r"^POST /receivables/\d+/pdf$", "Re-issued customer invoice PDF"),
    (r"^POST /reconciliation/account/new$", "Added bank account"),
    (r"^POST /reconciliation/import$", "Imported bank statement"),
    (r"^POST /reconciliation/post-line$", "Posted bank line to the ledger"),
    (r"^POST /reconciliation/match$", "Matched bank line"),
    (r"^POST /reconciliation/auto-match$", "Ran bank auto-match"),
    (r"^POST /reconciliation/unmatch/\d+$", "Unmatched bank line"),
    (r"^POST /reconciliation/delete/\d+$", "Deleted bank statement line"),
    (r"^POST /accounting/journal/manual$", "Posted manual journal entry"),
    (r"^POST /accounting/journal/\d+/delete$", "Deleted journal entry"),
    (r"^POST /accounting/rebuild$", "Rebuilt ledger"),
    (r"^POST /accounting/coa/new$", "Added chart of accounts entry"),
    (r"^POST /accounting/coa/\d+/toggle$", "Toggled account status"),
    (r"^POST /accounting/opening$", "Posted opening balances"),
    (r"^POST /reports/statutory/pay$", "Marked statutory paid"),
    (r"^POST /settings/users/new$", "Added user"),
    (r"^POST /settings/users/\d+/toggle$", "Toggled user active status"),
    (r"^POST /settings/users/\d+/password$", "Changed a passcode"),
    (r"^POST /settings/save$", "Updated settings"),
    (r"^POST /settings/backups/create$", "Created a backup"),
]
_COMPILED = [(re.compile(p), label) for p, label in ACTION_PATTERNS]


def _friendly_action(method: str, path: str) -> str:
    key = f"{method} {path}"
    for pat, label in _COMPILED:
        if pat.match(key):
            return label
    return key


def log_action(db, user, action: str, path: str = "", status_code: int = 200) -> None:
    """Record something the middleware wouldn't catch on its own — chiefly
    sensitive GETs like downloading a full backup of the books, which is a
    read but absolutely needs a trace."""
    try:
        db.add(M.AuditLog(
            user_id=user.id if user else None,
            user_name=user.display_name if user else "Unknown",
            method="GET", path=path, query="", action=action,
            status_code=status_code, blocked=False))
        db.commit()
    except Exception:
        db.rollback()


class AccessControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        method = request.method
        mutating = method in MUTATING_METHODS
        should_block_check = mutating and not path.startswith(BLOCK_EXEMPT_PREFIXES)
        should_log = (mutating or path == "/logout") and not path.startswith(NEVER_LOG_PREFIXES)

        uid_before = request.session.get("uid")
        blocked = False
        confined_to = ""     # a confined role wandering outside its area
        if uid_before and not path.startswith(("/static",) + PUBLIC_PREFIXES):
            db = SessionLocal()
            try:
                u = db.get(M.User, uid_before)
                if u and u.role == "viewer" and should_block_check:
                    blocked = not (u.can_approve and APPROVER_PATH.match(path))
                if u and u.role == "storekeeper" and should_block_check \
                        and STOREKEEPER_DENY.match(path):
                    blocked = True
                if u and u.role in CONFINED_ROLES and path != "/":
                    allowed, home = CONFINED_ROLES[u.role]
                    if not path.startswith(allowed):
                        confined_to = home
            finally:
                db.close()

        if confined_to:
            return RedirectResponse(confined_to, status_code=303)

        if blocked:
            # Send them back where they came from, not to `path` itself — most
            # mutating routes (e.g. POST /sales/new) have no GET handler of
            # their own and would 405 if the browser followed a redirect there.
            back = request.headers.get("referer") or "/"
            sep = "&" if "?" in back else "?"
            response = RedirectResponse(f"{back}{sep}viewonly=1", status_code=303)
        else:
            response = await call_next(request)

        if should_log:
            # After call_next, request.session reflects whatever the route did
            # (login sets uid, logout clears it) — scope is shared by
            # reference through the whole middleware chain regardless of
            # wrapping order, so this is reliable either way.
            uid_after = request.session.get("uid")
            effective_uid = uid_after or uid_before
            db = SessionLocal()
            try:
                user = db.get(M.User, effective_uid) if effective_uid else None
                if path == "/login":
                    label = "Login" if response.status_code in (302, 303) else "Failed login attempt"
                elif path == "/logout":
                    label = "Logout"
                else:
                    label = _friendly_action(method, path)
                if blocked:
                    label += " — blocked (view-only access)"
                db.add(M.AuditLog(
                    user_id=user.id if user else None,
                    user_name=user.display_name if user else "Unknown",
                    method=method, path=path, query=str(request.url.query or ""),
                    action=label, status_code=response.status_code, blocked=blocked))
                db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()

        return response
