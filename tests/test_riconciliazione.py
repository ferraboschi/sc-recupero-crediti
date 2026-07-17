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

from sqlalchemy import text

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


# ── C. La cascata: ogni scalino DEVE chiudere ────────────────────────

def _popola_tutte_le_sovrapposizioni(session):
    """Dati che esercitano OGNI sovrapposizione fra le categorie.

    È il punto del test: le categorie si sovrappongono nella realtà
    (orfana+contestata, escluso+contestato...). Se si sommassero categorie
    sovrapposte, la cascata non chiuderebbe — cioè avremmo ricostruito
    esattamente il bug che stiamo curando.

    Ritorna il totale dell'universo atteso.
    """
    escluso = _customer(session, "Escluso SRL", excluded=True)
    normale = _customer(session, "Normale SRL", recovery_status="first_contact")
    contestatore = _customer(session, "Contestatore SRL", recovery_status="idle")
    senza_stato = _customer(session, "Senza Stato SRL")
    # recovery_status NULL: riga legacy. Va forzato in SQL — passare None
    # al modello non basta, SQLAlchemy applicherebbe il default 'idle'.
    session.execute(
        text("UPDATE customers SET recovery_status = NULL WHERE id = :id"),
        {"id": senza_stato.id},
    )
    session.commit()

    # non_abbinati (orfane) — anche in sovrapposizione con 'contestata'
    _invoice(session, "ORF/1", amount_due=10.0, customer_id=None)
    _invoice(session, "ORF/2", amount_due=20.0, customer_id=None, status="disputed")

    # esclusi — anche in sovrapposizione con 'contestata'
    _invoice(session, "ESC/1", amount_due=30.0, customer_id=escluso.id)
    _invoice(session, "ESC/2", amount_due=40.0, customer_id=escluso.id,
             status="disputed")

    # contestati (abbinati, non esclusi)
    _invoice(session, "CON/1", amount_due=50.0, customer_id=contestatore.id,
             status="disputed")

    # lavorabile
    _invoice(session, "LAV/1", amount_due=60.0, customer_id=normale.id)
    _invoice(session, "LAV/2", amount_due=70.0, customer_id=senza_stato.id)

    # FUORI dall'universo: pagata, e non ancora scaduta
    _invoice(session, "PAID/1", amount_due=0.0, amount=500.0, status="paid",
             customer_id=normale.id, days_overdue=0)
    _invoice(session, "FUT/1", amount_due=900.0, customer_id=normale.id,
             days_overdue=0)

    return 10.0 + 20.0 + 30.0 + 40.0 + 50.0 + 60.0 + 70.0  # = 280


class TestIdentitaCascata:
    """IL TEST PIÙ IMPORTANTE: se la cascata non chiude, il lavoro non serve."""

    def test_identita_la_cascata_chiude(self, test_client, test_db_session):
        """scaduto_totale == non_abbinati + esclusi + contestati + lavorabile"""
        atteso = _popola_tutte_le_sovrapposizioni(test_db_session)

        d = test_client.get("/api/dashboard/riconciliazione").json()
        c = d["cascata"]

        somma = (
            c["non_abbinati"]["importo"]
            + c["esclusi"]["importo"]
            + c["contestati"]["importo"]
            + c["lavorabile"]["importo"]
        )
        assert c["scaduto_totale"]["importo"] == atteso
        assert somma == c["scaduto_totale"]["importo"], (
            f"LA CASCATA NON CHIUDE: {somma} != {c['scaduto_totale']['importo']}"
        )

    def test_precedenza_orfana_batte_contestata(self, test_client, test_db_session):
        """ORF/2 è orfana E contestata: conta UNA volta sola, fra le orfane."""
        _popola_tutte_le_sovrapposizioni(test_db_session)
        c = test_client.get("/api/dashboard/riconciliazione").json()["cascata"]

        assert c["non_abbinati"]["importo"] == 30.0  # ORF/1 + ORF/2
        # ORF/2 (20) NON è anche fra i contestati: lì c'è solo CON/1 (50)
        assert c["contestati"]["importo"] == 50.0

    def test_precedenza_escluso_batte_contestata(self, test_client, test_db_session):
        """ESC/2 è di un escluso E contestata: conta fra gli esclusi."""
        _popola_tutte_le_sovrapposizioni(test_db_session)
        c = test_client.get("/api/dashboard/riconciliazione").json()["cascata"]

        assert c["esclusi"]["importo"] == 70.0  # ESC/1 + ESC/2

    def test_lavorabile_coincide_col_motore(self, test_client, test_db_session):
        """Il lavorabile è ESATTAMENTE ciò che il motore lavora."""
        _popola_tutte_le_sovrapposizioni(test_db_session)
        c = test_client.get("/api/dashboard/riconciliazione").json()["cascata"]

        assert c["lavorabile"]["importo"] == 130.0  # LAV/1 + LAV/2

    def test_identita_stati_pratica(self, test_client, test_db_session):
        """lavorabile == somma degli stati pratica (clienti senza stato inclusi)."""
        _popola_tutte_le_sovrapposizioni(test_db_session)
        d = test_client.get("/api/dashboard/riconciliazione").json()
        c = d["cascata"]

        per_stato = c["lavorabile"]["per_stato"]
        somma = sum(s["importo"] for s in per_stato.values())
        assert somma == c["lavorabile"]["importo"], (
            f"gli stati non sommano al lavorabile: {somma} != {c['lavorabile']['importo']}"
        )
        # Il cliente con recovery_status NULL non si perde per strada
        assert per_stato["sconosciuto"]["importo"] == 70.0
        assert per_stato["first_contact"]["importo"] == 60.0

    def test_identita_su_db_vuoto(self, test_client):
        """La cascata chiude anche a zero (nessuna divisione per zero)."""
        c = test_client.get("/api/dashboard/riconciliazione").json()["cascata"]
        somma = (
            c["non_abbinati"]["importo"] + c["esclusi"]["importo"]
            + c["contestati"]["importo"] + c["lavorabile"]["importo"]
        )
        assert c["scaduto_totale"]["importo"] == 0.0
        assert somma == 0.0

    def test_conteggi_chiudono_come_gli_importi(self, test_client, test_db_session):
        """Non solo gli euro: anche il numero di fatture deve chiudere."""
        _popola_tutte_le_sovrapposizioni(test_db_session)
        c = test_client.get("/api/dashboard/riconciliazione").json()["cascata"]

        somma = (
            c["non_abbinati"]["fatture"] + c["esclusi"]["fatture"]
            + c["contestati"]["fatture"] + c["lavorabile"]["fatture"]
        )
        assert c["scaduto_totale"]["fatture"] == 7
        assert somma == 7


class TestRecuperato:
    """Recuperato certo (paid_at) vs storico stimato (updated_at): mai mischiati."""

    def test_recuperato_certo_usa_il_residuo(self, test_client, test_db_session):
        """Pagata dopo il primo sollecito: conta il RESIDUO, non l'importo pieno."""
        c = _customer(test_db_session, "Pagante")
        azione = RecoveryAction(
            customer_id=c.id, action_type="first_contact",
            created_at=datetime.utcnow() - timedelta(days=10),
            completed_at=datetime.utcnow() - timedelta(days=10),
        )
        test_db_session.add(azione)
        test_db_session.commit()

        _invoice(test_db_session, "P/1", amount=500.0, amount_due=0.0,
                 status="paid", days_overdue=0, customer_id=c.id,
                 paid_at=datetime.utcnow() - timedelta(days=1),
                 amount_due_at_paid=200.0)

        r = test_client.get("/api/dashboard/riconciliazione").json()["recuperato"]
        assert r["certo"]["importo"] == 200.0, "il residuo, non l'importo pieno"
        assert r["certo"]["stimato"] is False

    def test_pagata_prima_del_sollecito_non_e_recupero(self, test_client, test_db_session):
        """Pagata PRIMA della prima azione: non l'abbiamo recuperata noi."""
        c = _customer(test_db_session, "Spontaneo")
        azione = RecoveryAction(
            customer_id=c.id, action_type="first_contact",
            created_at=datetime.utcnow() - timedelta(days=1),
            completed_at=datetime.utcnow() - timedelta(days=1),
        )
        test_db_session.add(azione)
        test_db_session.commit()

        _invoice(test_db_session, "P/1", amount=500.0, amount_due=0.0,
                 status="paid", days_overdue=0, customer_id=c.id,
                 paid_at=datetime.utcnow() - timedelta(days=30),
                 amount_due_at_paid=200.0)

        r = test_client.get("/api/dashboard/riconciliazione").json()["recuperato"]
        assert r["certo"]["importo"] == 0.0

    def test_storico_ante_migrazione_e_marcato_stimato(self, test_client, test_db_session):
        """Senza paid_at la data di pagamento non esiste: è una STIMA, e si dice."""
        c = _customer(test_db_session, "Storico")
        azione = RecoveryAction(
            customer_id=c.id, action_type="first_contact",
            created_at=datetime.utcnow() - timedelta(days=10),
            completed_at=datetime.utcnow() - timedelta(days=10),
        )
        test_db_session.add(azione)
        test_db_session.commit()

        # Riga ante-migrazione: paid_at NULL, residuo già azzerato e perso
        _invoice(test_db_session, "OLD/1", amount=300.0, amount_due=0.0,
                 status="paid", days_overdue=0, customer_id=c.id)

        r = test_client.get("/api/dashboard/riconciliazione").json()["recuperato"]
        assert r["certo"]["importo"] == 0.0
        assert r["storico_stimato"]["importo"] == 300.0
        assert r["storico_stimato"]["stimato"] is True
        assert "stima" in r["storico_stimato"]["nota"].lower()

    def test_certo_e_stimato_non_si_sovrappongono(self, test_client, test_db_session):
        """Una fattura sta in UNO dei due secchielli, mai in entrambi."""
        c = _customer(test_db_session, "Misto")
        azione = RecoveryAction(
            customer_id=c.id, action_type="first_contact",
            created_at=datetime.utcnow() - timedelta(days=10),
            completed_at=datetime.utcnow() - timedelta(days=10),
        )
        test_db_session.add(azione)
        test_db_session.commit()

        _invoice(test_db_session, "NEW/1", amount=500.0, amount_due=0.0,
                 status="paid", days_overdue=0, customer_id=c.id,
                 paid_at=datetime.utcnow(), amount_due_at_paid=200.0)
        _invoice(test_db_session, "OLD/1", amount=300.0, amount_due=0.0,
                 status="paid", days_overdue=0, customer_id=c.id)

        r = test_client.get("/api/dashboard/riconciliazione").json()["recuperato"]
        assert r["certo"]["fatture"] == 1
        assert r["storico_stimato"]["fatture"] == 1
        assert r["certo"]["importo"] == 200.0
        assert r["storico_stimato"]["importo"] == 300.0
