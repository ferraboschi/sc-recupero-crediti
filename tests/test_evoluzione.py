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

Backfill STIMATO dello storico (sezioni D-G):
- la stima a un giorno D ricostruita dalle date fattura (aperte /
  pagate-con-paid_at / pagate-SENZA-paid_at, ESCLUSE dalla serie: updated_at
  viene bumpata dai sync e datarci il pagamento creava un gradino permanente
  alla giunzione stima→vero)
- lo scenario del cliff misurato: paid pre-migrazione toccate dal sync NON
  producono nessun gradino — stimato(ieri) == vero(oggi) per costruzione
- la classificazione di OGGI proiettata indietro (esclusi/contestati/orfane)
- il backfill NON sovrascrive mai righe esistenti (vere O stimate)
- lo snapshot vero (record_overdue_snapshot) RIMPIAZZA la stima sulla stessa
  data (estimated torna False, valori ricalcolati)
- marker one-shot in sync_state: secondo giro = skipped; scritto solo a
  successo (un fallimento riprova al prossimo avvio)
- GET /evoluzione espone `stimato` per punto
"""

from datetime import date, datetime, timedelta

from backend.database import (
    Invoice, Customer, RecoveryAction, OverdueSnapshot, SyncState,
)
from backend.engine.cases import business_day_start
from backend.engine.overdue_history import (
    record_overdue_snapshot,
    backfill_overdue_history,
    backfill_overdue_history_if_needed,
)


def _business_today():
    """La stessa àncora di data usata da snapshot e backfill."""
    return business_day_start().date()


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
                  recuperato_certo=0.0, estimated=False):
    """Inserisce direttamente una riga di storico (per i test dell'endpoint)."""
    snap = OverdueSnapshot(
        date=d,
        scaduto_totale=scaduto_totale,
        lavorabile=lavorabile,
        recuperato_certo=recuperato_certo,
        estimated=estimated,
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


# ── D. Il backfill stimato dello storico ─────────────────────────────

class TestBackfillStimato:
    def test_stima_a_un_giorno_con_mix(self, test_db_session):
        """La stima a un giorno D con il mix completo: aperta, pagata con
        paid_at, pagata SENZA paid_at (storico pre-migrazione).

        - A (aperta, cliente):    due=today-40, amount_due=100 → scaduta da D>=due
        - B (pagata, paid_at=−10gg, cliente): due=today-40, amount=200 pieno
          → scaduta per D in [today-40, paid_at), poi sparisce
        - C (pagata SENZA paid_at, ORFANA): ESCLUSA dalla serie a ogni data.
          Non ha una data di pagamento vera e updated_at NON è un proxy
          affidabile (i sync la bumpano di continuo): datarci il pagamento
          creava un gradino permanente alla giunzione stima→vero.
        """
        today = _business_today()
        now = datetime.utcnow()
        c = _customer(test_db_session, "Normale SRL")

        _invoice(test_db_session, "A/1", amount_due=100.0,
                 due_date=today - timedelta(days=40),
                 issue_date=today - timedelta(days=70),
                 customer_id=c.id)
        _invoice(test_db_session, "B/1", amount=200.0, amount_due=0.0,
                 status="paid", days_overdue=0,
                 due_date=today - timedelta(days=40),
                 issue_date=today - timedelta(days=70),
                 customer_id=c.id,
                 paid_at=now - timedelta(days=10))
        _invoice(test_db_session, "C/1", amount=300.0, amount_due=0.0,
                 status="paid", days_overdue=0,
                 due_date=today - timedelta(days=50),
                 issue_date=today - timedelta(days=80),
                 customer_id=None,
                 updated_at=now - timedelta(days=20))

        stats = backfill_overdue_history(test_db_session, days=60)
        test_db_session.commit()

        rows = {s.date: s for s in test_db_session.query(OverdueSnapshot).all()}
        # 60 giorni: da today-60 a ieri, tutti stimati
        assert len(rows) == 60
        assert stats["created"] == 60
        assert all(s.estimated for s in rows.values())

        # D=today-45: A e B non ancora scadute (due=today-40), C esclusa
        # dalla serie (paid senza paid_at) → il punto è a zero
        s45 = rows[today - timedelta(days=45)]
        assert s45.scaduto_totale == 0.0
        assert s45.non_abbinati == 0.0

        # D=today-30: A(100) + B(200, non ancora pagata); C mai contata
        s30 = rows[today - timedelta(days=30)]
        assert s30.scaduto_totale == 300.0
        assert s30.lavorabile == 300.0        # A + B (cliente normale)
        assert s30.non_abbinati == 0.0        # C esclusa, non "orfana aperta"
        assert s30.scaduto_totale_fatture == 2
        assert s30.lavorabile_fatture == 2

        # D=today-15: B ancora in circolo (paid_at −10gg) → A + B
        s15 = rows[today - timedelta(days=15)]
        assert s15.scaduto_totale == 300.0
        assert s15.non_abbinati == 0.0

        # D=today-5: anche B pagata (paid_at −10gg) → resta solo A
        s5 = rows[today - timedelta(days=5)]
        assert s5.scaduto_totale == 100.0
        assert s5.lavorabile == 100.0
        assert s5.scaduto_totale_fatture == 1

    def test_paid_con_paid_at_esce_alla_sua_data(self, test_db_session):
        """Le pagate CON paid_at (data di pagamento vera) continuano a
        uscire dallo scaduto stimato alla loro data: l'esclusione riguarda
        SOLO le paid senza paid_at."""
        today = _business_today()
        now = datetime.utcnow()
        _invoice(test_db_session, "PAY/1", amount=400.0, amount_due=0.0,
                 status="paid", days_overdue=0,
                 due_date=today - timedelta(days=25),
                 issue_date=today - timedelta(days=55),
                 paid_at=now - timedelta(days=10))

        backfill_overdue_history(test_db_session, days=30)
        test_db_session.commit()

        rows = {s.date: s for s in test_db_session.query(OverdueSnapshot).all()}
        assert rows[today - timedelta(days=15)].scaduto_totale == 400.0, \
            "ancora in circolo prima del pagamento"
        assert rows[today - timedelta(days=5)].scaduto_totale == 0.0, \
            "uscita dallo scaduto alla sua data di pagamento"

    def test_niente_gradino_alla_giunzione(self, test_db_session):
        """LO SCENARIO DEL CLIFF (misurato dal controagente): 150 paid
        pre-migrazione (senza paid_at) con updated_at bumpata OGGI da un
        sync — trigger plausibili in prod: _recalculate_days_overdue,
        repair, assign-piva.

        Con la convenzione updated_at la serie stimata le contava aperte
        per tutta la finestra: stimato(ieri) 1.097.681 € contro il punto
        vero di oggi 725.206 € — un gradino permanente del +51,4% alla
        giunzione (la promozione tocca solo la riga di oggi, mai il
        passato). ESCLUSE dalla stima, la giunzione è continua PER
        COSTRUZIONE: le pagate non compaiono né nella stima né nel vero.
        """
        today = _business_today()
        now = datetime.utcnow()
        c = _customer(test_db_session, "Grosso Cliente SRL")

        # Lo scaduto vero di oggi: 725.206 € su 3 fatture aperte
        for i, (ago, imp) in enumerate(
            [(60, 300000.0), (45, 300000.0), (30, 125206.0)]
        ):
            _invoice(test_db_session, f"OPN/{i}", amount_due=imp,
                     due_date=today - timedelta(days=ago),
                     issue_date=today - timedelta(days=ago + 30),
                     days_overdue=ago, customer_id=c.id)

        # 150 paid pre-migrazione (372.475,50 € pieni), toccate oggi dal sync
        for i in range(150):
            _invoice(test_db_session, f"OLD/{i}", amount=2483.17,
                     amount_due=0.0, status="paid", days_overdue=0,
                     due_date=today - timedelta(days=50 + (i % 30)),
                     issue_date=today - timedelta(days=90 + (i % 30)),
                     customer_id=c.id, updated_at=now)

        backfill_overdue_history(test_db_session, days=30)
        test_db_session.commit()
        vero_oggi = record_overdue_snapshot(test_db_session)

        ieri = (test_db_session.query(OverdueSnapshot)
                .filter(OverdueSnapshot.date == today - timedelta(days=1))
                .one())
        assert ieri.estimated is True
        assert vero_oggi.estimated is False
        # Nessun gradino: stimato(ieri) == vero(oggi), non 1.097.681 vs 725.206
        assert ieri.scaduto_totale == 725206.0
        assert vero_oggi.scaduto_totale == 725206.0
        assert ieri.scaduto_totale_fatture == 3
        assert vero_oggi.scaduto_totale_fatture == 3

    def test_classificazione_di_oggi_proiettata_indietro(self, test_db_session):
        """Esclusi e contestati di OGGI restano tali anche nella stima:
        la composizione è la classificazione attuale proiettata indietro."""
        today = _business_today()
        escluso = _customer(test_db_session, "Escluso SRL", excluded=True)

        _invoice(test_db_session, "ESC/1", amount_due=80.0,
                 due_date=today - timedelta(days=30),
                 customer_id=escluso.id)
        _invoice(test_db_session, "CON/1", amount_due=55.0,
                 status="disputed",
                 due_date=today - timedelta(days=30),
                 customer_id=None)

        backfill_overdue_history(test_db_session, days=20)
        test_db_session.commit()

        s10 = (test_db_session.query(OverdueSnapshot)
               .filter(OverdueSnapshot.date == today - timedelta(days=10))
               .one())
        assert s10.esclusi == 80.0
        # CON/1 è orfana E contestata: la gerarchia di bucket_expr() la
        # mette in non_abbinati (senza cliente la domanda "è esclusa?"
        # non ha risposta) — identica alla cascata live.
        assert s10.non_abbinati == 55.0
        assert s10.contestati == 0.0
        assert s10.lavorabile == 0.0
        assert s10.scaduto_totale == 135.0

    def test_recuperato_certo_storico_cumulato(self, test_db_session):
        """recuperato_certo(D) cumula le pagate con paid_at <= D che
        rispettano recovered_invoice_clause (stessi helper del live)."""
        today = _business_today()
        now = datetime.utcnow()
        c = _customer(test_db_session, "Pagante SRL")
        test_db_session.add(RecoveryAction(
            customer_id=c.id, action_type="first_contact",
            created_at=now - timedelta(days=30),
            completed_at=now - timedelta(days=30),
        ))
        test_db_session.commit()

        _invoice(test_db_session, "P/1", amount=300.0, amount_due=0.0,
                 status="paid", days_overdue=0,
                 issue_date=today - timedelta(days=60),
                 due_date=today - timedelta(days=40),
                 customer_id=c.id,
                 paid_at=now - timedelta(days=10),
                 amount_due_at_paid=150.0)

        backfill_overdue_history(test_db_session, days=30)
        test_db_session.commit()

        rows = {s.date: s for s in test_db_session.query(OverdueSnapshot).all()}
        # Prima del pagamento: nulla di recuperato
        assert rows[today - timedelta(days=15)].recuperato_certo == 0.0
        assert rows[today - timedelta(days=15)].recuperato_certo_fatture == 0
        # Dopo il pagamento: cumulato a residuo (amount_due_at_paid)
        assert rows[today - timedelta(days=5)].recuperato_certo == 150.0
        assert rows[today - timedelta(days=5)].recuperato_certo_fatture == 1

    def test_non_sovrascrive_righe_esistenti(self, test_db_session):
        """Le righe già presenti — VERE o stimate — non si toccano mai."""
        today = _business_today()
        _snapshot_row(test_db_session, today - timedelta(days=10),
                      scaduto_totale=42.0, estimated=False)
        _snapshot_row(test_db_session, today - timedelta(days=20),
                      scaduto_totale=777.0, estimated=True)
        _invoice(test_db_session, "O/1", amount_due=100.0,
                 due_date=today - timedelta(days=40))

        stats = backfill_overdue_history(test_db_session, days=30)
        test_db_session.commit()

        rows = {s.date: s for s in test_db_session.query(OverdueSnapshot).all()}
        assert len(rows) == 30
        assert stats["created"] == 28
        assert stats["skipped_existing"] == 2
        # La riga VERA è intatta
        vera = rows[today - timedelta(days=10)]
        assert vera.scaduto_totale == 42.0
        assert vera.estimated is False
        # Anche la stima già scritta non viene riscritta (idempotenza)
        stima = rows[today - timedelta(days=20)]
        assert stima.scaduto_totale == 777.0
        # Le date nuove sono state stimate davvero
        nuova = rows[today - timedelta(days=5)]
        assert nuova.scaduto_totale == 100.0
        assert nuova.estimated is True


# ── E. Lo snapshot vero rimpiazza la stima ───────────────────────────

class TestSnapshotVeroSostituisceStima:
    def test_upsert_vero_su_riga_stimata(self, test_db_session):
        """record_overdue_snapshot su una data con riga stimata: la riga
        diventa VERA (estimated=False) e i valori sono ricalcolati."""
        today = _business_today()
        _snapshot_row(test_db_session, today, scaduto_totale=999.0,
                      estimated=True)
        _invoice(test_db_session, "O/1", amount_due=100.0,
                 due_date=today - timedelta(days=30))

        record_overdue_snapshot(test_db_session)

        rows = (test_db_session.query(OverdueSnapshot)
                .filter(OverdueSnapshot.date == today).all())
        assert len(rows) == 1, "stessa riga, non un doppione"
        assert rows[0].estimated is False, "la stima è stata promossa a vera"
        assert rows[0].scaduto_totale == 100.0, "valori ricalcolati, non 999"


# ── F. Il trigger one-shot (marker in sync_state) ────────────────────

class TestTriggerOneShot:
    def test_marker_idempotente(self, test_db_session):
        """Primo giro: backfill + marker. Secondo giro: skipped, nessuna
        riga in più."""
        today = _business_today()
        _invoice(test_db_session, "O/1", amount_due=100.0,
                 due_date=today - timedelta(days=40))

        r1 = backfill_overdue_history_if_needed(test_db_session, days=30)
        assert r1.get("created") == 30
        marker = (test_db_session.query(SyncState)
                  .filter_by(key="overdue_history_backfill").first())
        assert marker is not None
        assert (marker.result or {}).get("done") is True

        r2 = backfill_overdue_history_if_needed(test_db_session, days=30)
        assert r2 == {"skipped": True}
        assert test_db_session.query(OverdueSnapshot).count() == 30

    def test_marker_scritto_solo_a_successo(self, monkeypatch, test_db_session):
        """Se il backfill esplode il marker NON viene scritto: al prossimo
        avvio si riprova (stesso pattern di cases.run_backfill_if_needed)."""
        import backend.engine.overdue_history as oh
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "get_session_direct",
                            lambda: test_db_session)

        def boom(session, days=90):
            raise RuntimeError("boom backfill")

        with monkeypatch.context() as m:
            m.setattr(oh, "backfill_overdue_history", boom)
            assert oh.run_history_backfill_if_needed() is None

        marker = (test_db_session.query(SyncState)
                  .filter_by(key="overdue_history_backfill").first())
        assert marker is None or not (marker.result or {}).get("done")

        # Il retry (senza l'esplosione) va a buon fine e scrive il marker
        result = oh.run_history_backfill_if_needed()
        assert result is not None and result.get("created", 0) >= 0
        marker = (test_db_session.query(SyncState)
                  .filter_by(key="overdue_history_backfill").first())
        assert (marker.result or {}).get("done") is True


# ── G. /evoluzione espone `stimato` ──────────────────────────────────

class TestEvoluzioneStimato:
    def test_espone_stimato_per_punto(self, test_client, test_db_session):
        """Ogni punto dice se è una stima o uno snapshot vero: è ciò che
        permette al grafico di tratteggiare lo storico ricostruito."""
        today = date.today()
        _snapshot_row(test_db_session, today - timedelta(days=2),
                      scaduto_totale=10.0, estimated=True)
        _snapshot_row(test_db_session, today - timedelta(days=1),
                      scaduto_totale=20.0, estimated=False)

        serie = test_client.get("/api/dashboard/evoluzione").json()["serie"]

        assert len(serie) == 2
        assert serie[0]["stimato"] is True
        assert serie[1]["stimato"] is False
