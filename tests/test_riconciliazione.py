"""Riconciliazione dei crediti: la cascata che DEVE chiudere.

Copertura:
- paid_at: la data di pagamento vera, valorizzata dal sync, azzerata alla
  riapertura; lo storico ante-migrazione resta NULL (non si inventa)
- amount_due_at_paid: il residuo fotografato all'atto del pagamento (il
  sync azzera amount_due, quindi il residuo va salvato PRIMA o è perso)
- definizione unica di "scaduto" (clausola SQL condivisa)
- l'identità della cascata: scaduto_totale == non_abbinati + esclusi +
  contestati + lavorabile, su dati che esercitano OGNI sovrapposizione
- /pipeline: total_with_overdue filtra gli esclusi come gli stage
"""

from datetime import date, datetime, timedelta

from backend.database import Invoice, Customer, RecoveryAction


# ── Helper ───────────────────────────────────────────────────────────

def _customer(session, name, excluded=False, recovery_status="idle"):
    c = Customer(
        ragione_sociale=name,
        excluded=excluded,
        recovery_status=recovery_status,
    )
    session.add(c)
    session.commit()
    return c


def _invoice(session, number, amount_due=100.0, status="open",
             days_overdue=30, customer_id=None, **kw):
    inv = Invoice(
        invoice_number=number,
        amount=kw.pop("amount", amount_due),
        amount_due=amount_due,
        issue_date=kw.pop("issue_date", date(2026, 4, 1)),
        due_date=kw.pop("due_date", date(2026, 5, 1)),
        days_overdue=days_overdue,
        status=status,
        customer_id=customer_id,
        source_platform=kw.pop("source_platform", "fatturapro"),
        **kw,
    )
    session.add(inv)
    session.commit()
    return inv


# ── A. La colonna paid_at ────────────────────────────────────────────

class TestPaidAtColumn:
    def test_paid_at_defaults_to_null(self, test_db_session):
        """Una fattura aperta non ha una data di pagamento."""
        inv = _invoice(test_db_session, "A/1")
        assert inv.paid_at is None
        assert inv.amount_due_at_paid is None

    def test_paid_at_set_when_sync_marks_paid(self, monkeypatch, test_db_session):
        """Il sync che marca 'paid' per assenza scrive anche paid_at."""
        from tests.test_sync_hardening import _run_invoice_sync, _raw, _mk_invoice

        _mk_invoice(test_db_session, "A/2026")
        _mk_invoice(test_db_session, "B/2026", missing_streak=1)

        before = datetime.utcnow()
        _run_invoice_sync(monkeypatch, test_db_session, [_raw("A/2026")])

        b = test_db_session.query(Invoice).filter_by(invoice_number="B/2026").one()
        assert b.status == "paid"
        assert b.paid_at is not None, "paid_at deve essere valorizzata al pagamento"
        assert b.paid_at >= before

    def test_residuo_snapshot_survives_the_zeroing(self, monkeypatch, test_db_session):
        """LA TRAPPOLA: il sync azzera amount_due quando marca paid.

        Sommare amount_due sulle pagate darebbe SEMPRE 0. Il residuo va
        fotografato prima dell'azzeramento, o il 'recuperato' non esiste.
        """
        from tests.test_sync_hardening import _run_invoice_sync, _raw, _mk_invoice

        _mk_invoice(test_db_session, "A/2026")
        _mk_invoice(test_db_session, "B/2026", missing_streak=1,
                    amount=250.0, amount_due=150.0)

        _run_invoice_sync(monkeypatch, test_db_session, [_raw("A/2026")])

        b = test_db_session.query(Invoice).filter_by(invoice_number="B/2026").one()
        assert b.amount_due == 0, "il sync azzera il residuo (comportamento esistente)"
        # Il residuo REALE recuperato era 150, non 250 (l'importo pieno)
        assert b.amount_due_at_paid == 150.0

    def test_paid_at_cleared_on_reopening(self, monkeypatch, test_db_session):
        """Fattura riaperta (ricompare con saldo>0): paid_at torna NULL.

        Una fattura riaperta NON è stata pagata: lasciare paid_at la
        farebbe contare per sempre nel recuperato.
        """
        from tests.test_sync_hardening import _run_invoice_sync, _raw, _mk_invoice

        _mk_invoice(test_db_session, "B/2026", status="paid", amount_due=0,
                    paid_at=datetime.utcnow(), amount_due_at_paid=100.0)

        _run_invoice_sync(monkeypatch, test_db_session, [_raw("B/2026", balance=100.0)])

        b = test_db_session.query(Invoice).filter_by(invoice_number="B/2026").one()
        assert b.status == "open"
        assert b.paid_at is None, "una fattura riaperta non è pagata"
        assert b.amount_due_at_paid is None

    def test_cleanup_f24_sets_paid_at(self, test_db_session, monkeypatch):
        """Anche l'altro punto che marca 'paid' valorizza paid_at."""
        import backend.api.sync as sync_mod
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: test_db_session)

        _invoice(test_db_session, "F24/1", amount_due=80.0,
                 source_platform="fatture24")

        import asyncio
        asyncio.run(sync_mod.cleanup_stale_f24())

        inv = test_db_session.query(Invoice).filter_by(invoice_number="F24/1").one()
        assert inv.status == "paid"
        assert inv.paid_at is not None
        assert inv.amount_due_at_paid == 80.0
