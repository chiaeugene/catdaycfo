"""Double-entry posting engine.

Auto journal entries are DERIVED from operational records (payments, vouchers,
sales, petty cash, payroll, statutory remittances) by deterministic posting
rules, keyed by (source_type, source_id, event). sync_ledger() reconciles the
journal against those rules on every accounting-report view: missing entries
are created, entries whose source vanished or changed state are removed. The
ledger therefore can never disagree with the operational screens — and can be
rebuilt from scratch at any time. Manual entries (Opening / Manual) persist.

Malaysian notes: SST paid on purchases is NOT an input-tax credit (unlike GST),
so purchase amounts post gross to expense; SST charged on sales posts to
2300 SST Payable.
"""
from datetime import date

from sqlalchemy.orm import Session

from . import models as M


def seed_coa(db: Session):
    """Idempotently create the seed Chart of Accounts, first migrating any
    old 4-digit codes to the SQL Account numbering (Weng Teng's template,
    Aug 2026). Accounts are renamed in place — same row id — so existing
    journal lines, and therefore every report, carry over untouched."""
    by_code = {a.code: a for a in db.query(M.Account).all()}
    changed = 0

    # 2230 EIS Payable merges into SOCSO & EIS (the template combines them):
    # move its journal lines across, then drop the account.
    old_eis = by_code.pop("2230", None)
    if old_eis is not None:
        target = by_code.get("2220") or by_code.get(M.ACC_SOCSO_EIS)
        if target is not None:
            db.query(M.JournalLine).filter(
                M.JournalLine.account_id == old_eis.id).update(
                {"account_id": target.id}, synchronize_session=False)
            db.delete(old_eis)
            changed += 1

    seed_names = {code: (name, typ) for code, name, typ in M.COA_SEED}
    for old, new in M.COA_RECODE.items():
        acc = by_code.get(old)
        if acc is not None and new not in by_code:
            acc.code = new
            if new in seed_names:
                acc.name, acc.type = seed_names[new]
            by_code[new] = by_code.pop(old)
            changed += 1
    if changed:
        db.commit()

    added = 0
    for code, name, typ in M.COA_SEED:
        if code not in by_code:
            db.add(M.Account(code=code, name=name, type=typ, is_system=True))
            added += 1
    if added:
        db.commit()
    return added


def _accounts_by_code(db: Session):
    return {a.code: a for a in db.query(M.Account).all()}


def _expense_account_code(category: str, section: str = "") -> str:
    if category in M.CATEGORY_ACCOUNT:
        return M.CATEGORY_ACCOUNT[category]
    # Uncategorized: physical purchase → equipment, else misc
    return M.ACC_EQUIPMENT if section == "Purchase" else M.ACC_MISC


def _expected_entries(db: Session):
    """Yield (key, builder) for every auto entry the current data implies.
    key = (source_type, source_id, event); builder() -> dict(date, ref, memo,
    month, lines=[(account_code, debit, credit, description)])."""
    out = {}

    for p in db.query(M.Payment).filter(M.Payment.status != "Void").all():
        if not p.amount:
            continue
        code = _expense_account_code(p.category or "", "Purchase" if p.grp == "CAPEX" else "")
        out[("Payment", p.id, "accrue")] = {
            "date": p.date, "ref": p.pay_no, "month": p.month,
            "memo": f"{p.supplier or 'Supplier'} · {p.description[:60]}",
            "lines": [(code, p.amount, 0, p.category or "Uncategorized"),
                      (M.ACC_AP, 0, p.amount, p.supplier or "")],
        }

    for v in db.query(M.Voucher).filter(M.Voucher.status == "Paid").all():
        if not v.total:
            continue
        out[("Voucher", v.id, "pay")] = {
            "date": v.date, "ref": v.pv_no, "month": f"{v.date:%b %Y}",
            "memo": f"Payment to {v.payee}",
            "lines": [(M.ACC_AP, v.total, 0, v.payee),
                      (M.ACC_BANK, 0, v.total, f"Bank payment {v.pv_no}")],
        }

    for s in db.query(M.SalesEntry).all():
        if not s.amount:
            continue
        cash_code = M.ACC_CASH if s.method == "Cash" else M.ACC_BANK
        tax = s.tax_amount or 0
        lines = [(cash_code, s.amount, 0, s.method)]
        lines.append((M.STREAM_ACCOUNT.get(s.stream, M.ACC_OTHER_INCOME), 0, s.amount - tax, s.stream))
        if tax:
            lines.append((M.ACC_SST, 0, tax, s.tax_type))
        out[("Sale", s.id, "post")] = {
            "date": s.date, "ref": s.stream, "month": s.month,
            "memo": f"Sale · {s.description[:60]}", "lines": lines,
        }

    for e in db.query(M.PettyCashEntry).all():
        lines = []
        if e.amount_in:
            lines += [(M.ACC_PETTY, e.amount_in, 0, "Top-up"), (M.ACC_BANK, 0, e.amount_in, "Top-up from bank")]
        if e.amount_out:
            lines += [(_expense_account_code(e.category or ""), e.amount_out, 0, e.category or "Petty spend"),
                      (M.ACC_PETTY, 0, e.amount_out, "Petty cash")]
        if not lines:
            continue
        out[("PettyCash", e.id, "post")] = {
            "date": e.date, "ref": "PC", "month": e.month,
            "memo": f"Petty cash · {e.description[:60]}", "lines": lines,
        }

    for run in db.query(M.PayrollRun).filter(M.PayrollRun.status == "Confirmed").all():
        gross = sum(i.gross for i in run.items)
        er = sum(i.epf_er + i.socso_er + i.eis_er for i in run.items)
        epf = sum(i.epf_er + i.epf_ee for i in run.items)
        socso = sum(i.socso_er + i.socso_ee for i in run.items)
        eis = sum(i.eis_er + i.eis_ee for i in run.items)
        pcb = sum(i.pcb for i in run.items)
        other = sum(i.deductions for i in run.items)
        net = sum(i.net for i in run.items)
        lines = [(M.ACC_SALARIES, gross, 0, "Gross salaries"),
                 (M.ACC_EMPLOYER_STAT, er, 0, "Employer EPF/SOCSO/EIS")]
        # SOCSO and EIS share one payable account per the template's chart.
        for code, amt, d in [(M.ACC_EPF, epf, "EPF"), (M.ACC_SOCSO_EIS, socso, "SOCSO"),
                             (M.ACC_SOCSO_EIS, eis, "EIS"), (M.ACC_PCB, pcb, "PCB"),
                             (M.ACC_OTHER_DED, other, "Other deductions")]:
            if amt:
                lines.append((code, 0, amt, d))
        lines.append((M.ACC_BANK, 0, net, "Net pay to staff"))
        out[("Payroll", run.id, "post")] = {
            "date": run.run_date, "ref": f"PAYROLL-{run.month}", "month": run.month,
            "memo": f"Payroll {run.month} ({len(run.items)} staff)", "lines": lines,
        }

    # Bank-in slips: money physically deposited into the bank. The revenue was
    # already recognised when the sale was recorded — this entry just moves the
    # cash (Dr Bank, Cr wherever it was sitting). The credit side is chosen at
    # verification: 1010 Cash on Hand for daily takings, or 1015 Cash & TNG
    # Clearing when banking in the pre-31-Jul opening balance.
    import json as _json
    for d in db.query(M.Document).filter(M.Document.status == "Verified",
                                         M.Document.section == "Bank-in Slip",
                                         M.Document.amount > 0).all():
        credit = M.ACC_CASH
        try:
            credit = _json.loads(d.payload_json or "{}").get("bankin_credit") or M.ACC_CASH
        except ValueError:
            pass
        # Old documents verified before the renumbering stored 4-digit codes.
        credit = M.COA_RECODE.get(credit, credit)
        if credit not in (M.ACC_CASH, M.ACC_TNG_CLEARING):
            credit = M.ACC_CASH
        entry_date = d.doc_date or (d.verified_at.date() if d.verified_at
                                    else d.received_at.date())
        out[("Document", d.id, "bankin")] = {
            "date": entry_date,
            "ref": d.doc_no, "month": d.month,
            "memo": f"Bank-in · {d.description[:60]}",
            "lines": [(M.ACC_BANK, d.amount, 0, "Deposited to bank"),
                      (credit, 0, d.amount, "Cash banked in")],
        }

    # Receivables: invoice recognises revenue on credit; receipt collects it.
    for inv in db.query(M.ARInvoice).filter(M.ARInvoice.status != "Void").all():
        if not inv.amount:
            continue
        out[("ARInvoice", inv.id, "invoice")] = {
            "date": inv.date, "ref": inv.inv_no, "month": inv.month or f"{inv.date:%b %Y}",
            "memo": f"Invoice {inv.inv_no} · {inv.customer}",
            "lines": [(M.ACC_AR, inv.amount, 0, inv.customer),
                      (M.STREAM_ACCOUNT.get(inv.stream, M.ACC_OTHER_INCOME), 0, inv.amount, inv.stream)],
        }
        for r in inv.receipts:
            if not r.amount:
                continue
            cash_code = M.ACC_CASH if r.method == "Cash" else M.ACC_BANK
            out[("ARReceipt", r.id, "receive")] = {
                "date": r.date, "ref": inv.inv_no, "month": f"{r.date:%b %Y}",
                "memo": f"Receipt for {inv.inv_no} · {inv.customer}",
                "lines": [(cash_code, r.amount, 0, r.method),
                          (M.ACC_AR, 0, r.amount, inv.customer)],
            }

    kind_acct = {"EPF": M.ACC_EPF, "SOCSO": M.ACC_SOCSO_EIS, "EIS": M.ACC_SOCSO_EIS,
                 "PCB": M.ACC_PCB}
    for sp in db.query(M.StatutoryPaid).all():
        if not sp.amount or sp.kind not in kind_acct:
            continue
        out[("Statutory", sp.id, "pay")] = {
            "date": sp.paid_date, "ref": f"{sp.kind}-{sp.month}", "month": sp.month,
            "memo": f"{sp.kind} remittance for {sp.month}",
            "lines": [(kind_acct[sp.kind], sp.amount, 0, sp.kind),
                      (M.ACC_BANK, 0, sp.amount, f"Paid to authority")],
        }
    return out


def sync_ledger(db: Session):
    """Reconcile auto journal entries against posting rules. Returns (added, removed)."""
    accounts = _accounts_by_code(db)
    expected = _expected_entries(db)
    existing = {}
    for je in db.query(M.JournalEntry).filter(
            M.JournalEntry.source_type.notin_(("Manual", "Opening"))).all():
        existing[(je.source_type, je.source_id, je.event)] = je

    removed = 0
    for key, je in existing.items():
        if key not in expected:
            db.delete(je)
            removed += 1

    added = 0
    for key, spec in expected.items():
        if key in existing:
            continue
        st, sid, event = key
        je = M.JournalEntry(date=spec["date"], ref=spec["ref"], memo=spec["memo"],
                            source_type=st, source_id=sid, event=event,
                            month=spec["month"] or f"{spec['date']:%b %Y}",
                            created_by="system")
        for code, dr, cr, desc in spec["lines"]:
            acc = accounts.get(code)
            if not acc:
                continue
            je.lines.append(M.JournalLine(account_id=acc.id,
                                          debit=round(dr, 2), credit=round(cr, 2),
                                          description=desc))
        db.add(je)
        added += 1
    if added or removed:
        db.commit()
    return added, removed


def rebuild_ledger(db: Session):
    """Delete ALL auto entries and re-derive from scratch (manual entries kept)."""
    for je in db.query(M.JournalEntry).filter(
            M.JournalEntry.source_type.notin_(("Manual", "Opening"))).all():
        db.delete(je)
    db.commit()
    return sync_ledger(db)


def post_manual(db: Session, entry_date: date, memo: str, lines, user: str,
                source_type: str = "Manual", ref: str = ""):
    """lines = [(account_id, debit, credit, description)]. Must balance."""
    lines = [(aid, round(dr or 0, 2), round(cr or 0, 2), desc)
             for aid, dr, cr, desc in lines if (dr or 0) or (cr or 0)]
    if not lines:
        raise ValueError("No lines")
    if abs(sum(l[1] for l in lines) - sum(l[2] for l in lines)) > 0.005:
        raise ValueError("Entry does not balance")
    je = M.JournalEntry(date=entry_date, ref=ref, memo=memo, source_type=source_type,
                        source_id=0, event="post", month=f"{entry_date:%b %Y}",
                        created_by=user)
    for aid, dr, cr, desc in lines:
        je.lines.append(M.JournalLine(account_id=aid, debit=dr, credit=cr, description=desc))
    db.add(je)
    db.commit()
    return je


def trial_balance(db: Session, as_of: date):
    """[(account, dr_total, cr_total, balance)] for accounts with activity, + totals."""
    sync_ledger(db)
    rows = []
    tot_dr = tot_cr = 0.0
    for acc in db.query(M.Account).filter(M.Account.active == True).order_by(M.Account.code).all():  # noqa: E712
        dr = cr = 0.0
        for line in db.query(M.JournalLine).join(M.JournalEntry).filter(
                M.JournalLine.account_id == acc.id, M.JournalEntry.date <= as_of).all():
            dr += line.debit
            cr += line.credit
        if abs(dr) < 0.005 and abs(cr) < 0.005:
            continue
        bal = round(dr - cr, 2)
        rows.append({"account": acc, "dr": round(dr, 2), "cr": round(cr, 2),
                     "bal_dr": bal if bal > 0 else 0, "bal_cr": -bal if bal < 0 else 0})
        tot_dr += bal if bal > 0 else 0
        tot_cr += -bal if bal < 0 else 0
    return rows, round(tot_dr, 2), round(tot_cr, 2)


def trial_balance_range(db: Session, d_from: date, d_to: date):
    """Period trial balance: opening balance (before d_from), period movement,
    closing balance per account. Returns (rows, totals dict)."""
    sync_ledger(db)
    rows = []
    tot = {"open_dr": 0.0, "open_cr": 0.0, "dr": 0.0, "cr": 0.0,
           "close_dr": 0.0, "close_cr": 0.0}
    for acc in db.query(M.Account).filter(M.Account.active == True).order_by(M.Account.code).all():  # noqa: E712
        opening = pdr = pcr = 0.0
        for line in db.query(M.JournalLine).join(M.JournalEntry).filter(
                M.JournalLine.account_id == acc.id, M.JournalEntry.date <= d_to).all():
            if line.entry.date < d_from:
                opening += line.debit - line.credit
            else:
                pdr += line.debit
                pcr += line.credit
        closing = opening + pdr - pcr
        if abs(opening) < 0.005 and abs(pdr) < 0.005 and abs(pcr) < 0.005:
            continue
        row = {"account": acc,
               "open_dr": round(opening, 2) if opening > 0 else 0,
               "open_cr": round(-opening, 2) if opening < 0 else 0,
               "dr": round(pdr, 2), "cr": round(pcr, 2),
               "close_dr": round(closing, 2) if closing > 0 else 0,
               "close_cr": round(-closing, 2) if closing < 0 else 0}
        rows.append(row)
        for k in tot:
            tot[k] += row[k]
    for k in tot:
        tot[k] = round(tot[k], 2)
    return rows, tot


def balance_sheet(db: Session, as_of: date):
    """Assets / Liabilities / Equity sections with current earnings folded in."""
    rows, _, _ = trial_balance(db, as_of)   # sync happens inside
    assets, liabilities, equity = [], [], []
    earnings = 0.0   # income − COGS − expenses, folded into equity as current earnings
    for r in rows:
        bal = r["bal_dr"] - r["bal_cr"]
        t = r["account"].type
        if t == "Asset":
            assets.append((r["account"], round(bal, 2)))
        elif t == "Liability":
            liabilities.append((r["account"], round(-bal, 2)))
        elif t == "Equity":
            equity.append((r["account"], round(-bal, 2)))
        elif t == "Income":
            earnings += -bal
        else:   # COGS / Expense
            earnings -= bal
    total_assets = round(sum(b for _, b in assets), 2)
    total_liab = round(sum(b for _, b in liabilities), 2)
    total_equity = round(sum(b for _, b in equity) + earnings, 2)
    return {"assets": assets, "liabilities": liabilities, "equity": equity,
            "earnings": round(earnings, 2), "total_assets": total_assets,
            "total_liabilities": total_liab, "total_equity": total_equity,
            "balanced": abs(total_assets - total_liab - total_equity) < 0.01}
