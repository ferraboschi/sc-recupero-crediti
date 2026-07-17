"""Storico dello scaduto: la serie temporale per il grafico di evoluzione.

Oggi la dashboard fotografa solo l'istante presente. Qui si persiste la
cascata giorno per giorno (uno snapshot al giorno) così l'evoluzione dello
scaduto — totale, lavorabile, recuperato — diventa una serie storica.

Copertura:
- record_overdue_snapshot scrive UNA riga per il giorno lavorativo corrente
- idempotenza: due sync nello stesso giorno aggiornano la stessa riga (UPSERT)
- la "data di oggi" viene da business_day_start(), non da date.today() UTC
- gli importi dello snapshot sono ESATTAMENTE la cascata di /riconciliazione
  (definizione condivisa: non possono divergere)
- recuperato_certo è cumulato (pagato dopo il primo sollecito, a residuo)
- GET /evoluzione ordina per data crescente e rispetta ?giorni
- storico vuoto → serie vuota, mai un 500
- il full sync scrive lo snapshot, ma se la scrittura esplode NON fa fallire
  il sync (si logga e si prosegue)
"""

from datetime import date, datetime, timedelta

from backend.database import Invoice, Customer, RecoveryAction, OverdueSnapshot
from backend.engine.overdue_history import record_overdue_snapshot


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


def _snapshot_row(session, d, scaduto_totale=0.0, lavorabile=0.0,
                  recuperato_certo=0.0):
    """Inserisce direttamente una riga di storico (per i test dell'endpoint)."""
    snap = OverdueSnapshot(
        date=d,
        scaduto_totale=scaduto_totale,
        lavorabile=lavorabile,
        recuperato_certo=recuperato_certo,
    )
    session.add(snap)
    session.commit()
    return snap


# ── A. La scrittura dello snapshot ───────────────────────────────────

class TestRecordSnapshot:
    def test_crea_una_riga_per_oggi(self, test_db_session):
        """record_overdue_snapshot crea uno snapshot con la data odierna."""
        _invoice(test_db_session, "O/1", amount_due=100.0)

        snap = record_overdue_snapshot(test_db_session)

        rows = test_db_session.query(OverdueSnapshot).all()
        assert len(rows) == 1
        assert snap.date is not None
        assert snap.scaduto_totale == 100.0

    def test_idempotente_stesso_giorno(self, test_db_session):
        """LA TRAPPOLA: due sync nello stesso giorno NON creano due righe.

        L'upsert aggiorna la riga del giorno: cambia il dato fra le due
        chiamate e lo snapshot deve riflettere la SECONDA, restando unico.
        """
        _invoice(test_db_session, "O/1", amount_due=100.0)
        record_overdue_snapshot(test_db_session)

        # Arriva altro scaduto e si risincronizza nello stesso giorno
        _invoice(test_db_session, "O/2", amount_due=50.0)
        record_overdue_snapshot(test_db_session)

        rows = test_db_session.query(OverdueSnapshot).all()
        assert len(rows) == 1, "un solo snapshot per giorno"
        assert rows[0].scaduto_totale == 150.0, "riflette il secondo sync"

    def test_usa_business_day_non_utc(self, monkeypatch, test_db_session):
        """La data di oggi viene da business_day_start(), non da date.today().

        Sul server UTC il confine di giornata cadrebbe all'1-2 di notte
        italiane: usare la stessa funzione del resto del sistema è il punto.
        """
        import backend.engine.overdue_history as oh
        fixed = datetime(2026, 3, 15, 12, 0, 0)
        monkeypatch.setattr(oh, "business_day_start", lambda: fixed)

        _invoice(test_db_session, "O/1", amount_due=100.0)
        snap = record_overdue_snapshot(test_db_session)

        assert snap.date == date(2026, 3, 15)

    def test_importi_coincidono_con_la_cascata(self, test_client, test_db_session):
        """Gli importi dello snapshot sono ESATTAMENTE quelli di /riconciliazione.

        Condividono compute_overdue_buckets: se un giorno divergessero, la
        serie storica racconterebbe una bugia rispetto al numero live.
        """
        escluso = _customer(test_db_session, "Escluso SRL", excluded=True)
        normale = _customer(test_db_session, "Normale SRL",
                            recovery_status="first_contact")
        _invoice(test_db_session, "ORF/1", amount_due=10.0, customer_id=None)
        _invoice(test_db_session, "ESC/1", amount_due=30.0,
                 customer_id=escluso.id)
        _invoice(test_db_session, "CON/1", amount_due=50.0,
                 status="disputed")
        _invoice(test_db_session, "LAV/1", amount_due=60.0,
                 customer_id=normale.id)

        snap = record_overdue_snapshot(test_db_session)
        casc = test_client.get("/api/dashboard/riconciliazione").json()["cascata"]

        assert snap.scaduto_totale == casc["scaduto_totale"]["importo"]
        assert snap.non_abbinati == casc["non_abbinati"]["importo"]
        assert snap.esclusi == casc["esclusi"]["importo"]
        assert snap.contestati == casc["contestati"]["importo"]
        assert snap.lavorabile == casc["lavorabile"]["importo"]
        # Anche i conteggi
        assert snap.scaduto_totale_fatture == casc["scaduto_totale"]["fatture"]
        assert snap.lavorabile_fatture == casc["lavorabile"]["fatture"]

    def test_recuperato_certo_cumulato(self, test_client, test_db_session):
        """recuperato_certo = pagato dopo il primo sollecito, a residuo."""
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

        snap = record_overdue_snapshot(test_db_session)
        rec = test_client.get(
            "/api/dashboard/riconciliazione"
        ).json()["recuperato"]

        assert snap.recuperato_certo == 200.0
        assert snap.recuperato_certo == rec["certo"]["importo"]
        assert snap.recuperato_certo_fatture == 1

    def test_snapshot_su_db_vuoto_non_esplode(self, test_db_session):
        """Nessuna fattura: lo snapshot è tutto a zero, non un errore."""
        snap = record_overdue_snapshot(test_db_session)
        assert snap.scaduto_totale == 0.0
        assert snap.lavorabile == 0.0
        assert snap.recuperato_certo == 0.0


# ── B. L'endpoint /evoluzione ────────────────────────────────────────

class TestEvoluzioneEndpoint:
    def test_storico_vuoto_serie_vuota(self, test_client):
        """Nessuno snapshot ancora: serie vuota, MAI un 500."""
        resp = test_client.get("/api/dashboard/evoluzione")
        assert resp.status_code == 200
        body = resp.json()
        assert body["serie"] == []

    def test_ordina_per_data_crescente(self, test_client, test_db_session):
        """Le righe tornano ordinate per data, anche se inserite alla rinfusa."""
        today = date.today()
        _snapshot_row(test_db_session, today - timedelta(days=1),
                      scaduto_totale=200.0)
        _snapshot_row(test_db_session, today - timedelta(days=3),
                      scaduto_totale=400.0)
        _snapshot_row(test_db_session, today - timedelta(days=2),
                      scaduto_totale=300.0)

        serie = test_client.get(
            "/api/dashboard/evoluzione"
        ).json()["serie"]

        date_ordinate = [p["data"] for p in serie]
        assert date_ordinate == sorted(date_ordinate)
        assert len(serie) == 3
        assert serie[0]["scaduto_totale"] == 400.0  # il più vecchio per primo

    def test_rispetta_giorni(self, test_client, test_db_session):
        """?giorni taglia lo storico più vecchio della finestra."""
        today = date.today()
        _snapshot_row(test_db_session, today - timedelta(days=1),
                      scaduto_totale=100.0)
        _snapshot_row(test_db_session, today - timedelta(days=100),
                      scaduto_totale=999.0)

        serie = test_client.get(
            "/api/dashboard/evoluzione?giorni=30"
        ).json()["serie"]

        totali = [p["scaduto_totale"] for p in serie]
        assert 100.0 in totali
        assert 999.0 not in totali, "fuori dalla finestra di 30 giorni"

    def test_forma_del_punto(self, test_client, test_db_session):
        """Ogni punto espone le serie che servono al grafico."""
        _snapshot_row(test_db_session, date.today(),
                      scaduto_totale=500.0, lavorabile=300.0,
                      recuperato_certo=120.0)

        serie = test_client.get(
            "/api/dashboard/evoluzione"
        ).json()["serie"]

        punto = serie[0]
        for k in ("data", "scaduto_totale", "lavorabile", "recuperato_certo"):
            assert k in punto
        assert punto["scaduto_totale"] == 500.0
        assert punto["lavorabile"] == 300.0
        assert punto["recuperato_certo"] == 120.0


# ── C. L'aggancio al sync ────────────────────────────────────────────

class TestSyncWiring:
    def test_full_sync_scrive_lo_snapshot(self, monkeypatch, test_db_session):
        """Il full sync, normalmente, scrive uno snapshot dello scaduto."""
        from backend.api import sync as sync_mod
        from tests.test_sync_automation import _patch_steps

        calls = []
        _patch_steps(monkeypatch, calls)
        monkeypatch.setattr(sync_mod, "get_session_direct",
                            lambda: test_db_session)

        _invoice(test_db_session, "O/1", amount_due=100.0)

        sync_mod._full_sync_task(include_order_matching=False)

        rows = test_db_session.query(OverdueSnapshot).all()
        assert len(rows) == 1
        assert rows[0].scaduto_totale == 100.0

    def test_snapshot_che_esplode_non_ferma_il_sync(self, monkeypatch,
                                                    test_db_session):
        """Se la scrittura snapshot esplode, il sync prosegue (si logga)."""
        from backend.api import sync as sync_mod
        import backend.engine.overdue_history as oh
        from tests.test_sync_automation import _patch_steps

        calls = []
        _patch_steps(monkeypatch, calls)
        monkeypatch.setattr(sync_mod, "get_session_direct",
                            lambda: test_db_session)

        def boom(session):
            raise RuntimeError("boom snapshot")

        monkeypatch.setattr(oh, "record_overdue_snapshot", boom)

        results = sync_mod._full_sync_task(include_order_matching=False)

        # Il sync è arrivato in fondo nonostante lo snapshot esploso
        for step in ("invoices", "customers", "matching", "auto_create",
                     "cases"):
            assert step in results
        assert sync_mod._sync_progress["running"] is False
