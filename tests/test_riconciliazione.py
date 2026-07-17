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


# ── B. La definizione unica di "scaduto" ─────────────────────────────

class TestDefinizioneScaduto:
    """Due definizioni incompatibili convivevano: is_overdue_unpaid (che
    esclude i disputed, usata dal motore) e 'days_overdue > 0' ricopiata a
    mano nei KPI (che li include). Da qui il todo che non si può chiudere.
    """

    def test_universo_e_lavorabile_sono_definizioni_diverse(self, test_db_session):
        """Il contestato è nell'universo dello scaduto ma NON è lavorabile."""
        from backend.engine.overdue import is_overdue_unpaid

        c = _customer(test_db_session, "ACME")
        disputed = _invoice(test_db_session, "D/1", status="disputed",
                            customer_id=c.id)
        # Il motore lo rifiuta...
        assert is_overdue_unpaid(disputed) is False
        # ...ma resta un credito scaduto e non pagato: l'universo lo contiene.
        assert disputed.status != "paid" and disputed.days_overdue > 0

    def test_cases_riusa_la_stessa_definizione(self):
        """cases.is_overdue_unpaid non è più una seconda implementazione."""
        from backend.engine.cases import is_overdue_unpaid as from_cases
        from backend.engine.overdue import is_overdue_unpaid as canonical
        assert from_cases is canonical

    def test_todo_non_chiudibile_sparisce(self, test_client, test_db_session):
        """IL BUG DEL PROPRIETARIO: un cliente con SOLE fatture contestate
        compariva nei todo, ma 'Copia Messaggio' rispondeva no_overdue e il
        todo tornava il giorno dopo. Il motore lo rifiuta → non è un todo.
        """
        c = _customer(test_db_session, "Contestatore", recovery_status="idle")
        _invoice(test_db_session, "D/1", status="disputed", customer_id=c.id)

        data = test_client.get("/api/dashboard/todos").json()
        ids = [t["customer_id"] for t in data["todos"]]
        assert c.id not in ids

    def test_todo_resta_se_ha_anche_scaduto_vero(self, test_client, test_db_session):
        """Contestata + scaduta vera: il todo resta, ma conta solo il vero."""
        c = _customer(test_db_session, "Misto", recovery_status="idle")
        _invoice(test_db_session, "D/1", status="disputed", amount_due=999.0,
                 customer_id=c.id)
        _invoice(test_db_session, "O/1", status="open", amount_due=100.0,
                 customer_id=c.id)

        data = test_client.get("/api/dashboard/todos").json()
        todo = next(t for t in data["todos"] if t["customer_id"] == c.id)
        assert todo["total_overdue"] == 100.0, "la contestata non gonfia il todo"

    def test_headline_resta_l_universo(self, test_client, test_db_session):
        """total_scaduto = CIMA della cascata (include contestati/esclusi/
        orfane), così coincide con scaduto_totale di /riconciliazione.

        È la scelta che fa quadrare i conti: il numero di testa deve essere
        esattamente quello che la cascata sotto spiega, riga per riga.
        """
        c = _customer(test_db_session, "Escluso", excluded=True)
        _invoice(test_db_session, "E/1", amount_due=50.0, customer_id=c.id)
        _invoice(test_db_session, "D/1", amount_due=30.0, status="disputed")
        _invoice(test_db_session, "O/1", amount_due=20.0)

        data = test_client.get("/api/dashboard").json()
        assert data["total_scaduto"] == 100.0
