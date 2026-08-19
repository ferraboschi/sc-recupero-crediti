"""Chiude il ciclo "l'incasso non si vede" (batch 2026-08-19).

Due funzioni:
1. POST /sync/reconcile-incassi — riconciliazione IMMEDIATA su richiesta:
   esegue il passo fatture+rilevamento pagamenti DUE VOLTE di fila, così una
   fattura già sparita dalla lista FatturaPro (incasso registrato) viene
   marcata pagata SUBITO — invece di attendere due sync orari
   (PAID_ABSENCE_STREAK=2 resta invariata: il doppio passaggio la aggira).
   - su fetch PARZIALE non marca nulla e lo dice (eredita il gate del task);
   - se un sync completo è già in corso, degrada con grazia (niente deadlock,
     niente doppio rilevamento in parallelo).
2. Trasparenza per fattura — il serializer di /customers/{id} espone
   missing_streak, updated_at, paid_at, così l'owner vede a colpo d'occhio
   dov'è ogni fattura senza più chiedere "è registrata?".
"""

from datetime import date, datetime

from backend.api import sync as sync_mod
from backend.database import Invoice, Customer

# Riuso degli helper dei test di sync/riconciliazione.
from tests.test_sync_hardening import _mk_invoice, _raw
from tests.test_riconciliazione import _customer, _invoice


# ── Harness: connettore FatturaPro finto con partial configurabile ───

def _setup_fatturapro(monkeypatch, session, raw_invoices, partial=False):
    """Reindirizza _sync_invoices_task a un connettore finto e alla sessione
    di test. `partial` controlla il flag di completezza della lista."""

    class Fake:
        def login(self):
            return True

        def fetch_overdue_invoices(self):
            return list(raw_invoices), partial

        def fetch_scadenze_map(self, target_keys=None, max_pages=400, patience=20):
            return {}, True

        def fetch_clienti_map(self):
            return {}, True

        def close(self):
            pass

    monkeypatch.setattr(sync_mod, "FatturaProConnector", lambda *a, **k: Fake())
    monkeypatch.setattr(sync_mod, "get_session_direct", lambda: session)


# ── FUNZIONE 1 — reconcile-incassi ───────────────────────────────────

class TestReconcileIncassi:
    def test_two_passes_mark_absent_invoice_paid(self, monkeypatch, test_db_session):
        """L'incasso si vede SUBITO: due passaggi completi portano una fattura
        assente da streak 0 → 2 → pagata, senza aspettare due sync orari."""
        _mk_invoice(test_db_session, "A/2026")
        _mk_invoice(test_db_session, "B/2026")  # missing_streak default 0

        _setup_fatturapro(monkeypatch, test_db_session, [_raw("A/2026")])
        result = sync_mod._reconcile_incassi_task()

        assert result["passes"] == 2
        assert result["marked_paid"] == 1
        assert result["partial"] is False
        b = test_db_session.query(Invoice).filter_by(invoice_number="B/2026").one()
        assert b.status == "paid"
        assert b.amount_due == 0
        assert b.paid_at is not None

    def test_present_invoice_not_marked(self, monkeypatch, test_db_session):
        """Una fattura ancora nella lista NON viene toccata: nessun incasso."""
        _mk_invoice(test_db_session, "A/2026")
        _setup_fatturapro(monkeypatch, test_db_session, [_raw("A/2026")])

        result = sync_mod._reconcile_incassi_task()
        assert result["marked_paid"] == 0
        assert result["passes"] == 2
        a = test_db_session.query(Invoice).filter_by(invoice_number="A/2026").one()
        assert a.status == "open"

    def test_partial_fetch_marks_nothing_and_is_honest(self, monkeypatch, test_db_session):
        """SICUREZZA: lista incompleta → non marca NULLA (nemmeno la fattura
        già a streak 1) e restituisce un messaggio onesto."""
        _mk_invoice(test_db_session, "B/2026", missing_streak=1)

        _setup_fatturapro(monkeypatch, test_db_session, [], partial=True)
        result = sync_mod._reconcile_incassi_task()

        assert result["partial"] is True
        assert result["marked_paid"] == 0
        # Si ferma al primo passaggio parziale: inutile ritentare subito.
        assert result["passes"] == 1
        assert "incompleta" in result["message"].lower()
        b = test_db_session.query(Invoice).filter_by(invoice_number="B/2026").one()
        assert b.status == "open"
        assert b.missing_streak == 1  # invariata: nessuna prova di pagamento

    def test_lock_busy_degrades_without_deadlock(self, monkeypatch, test_db_session):
        """LOCK: se un sync completo è già in corso, l'endpoint degrada con un
        messaggio, NON esegue il rilevamento in parallelo, NON va in deadlock."""
        called = []

        def spy():
            called.append(1)
            return {"fatturapro": {"partial": False, "paid_detected": 0}}

        monkeypatch.setattr(sync_mod, "_sync_invoices_task", spy)

        # Simula un sync completo in corso: il lock è già preso.
        acquired = sync_mod._sync_lock.acquire(blocking=False)
        assert acquired
        try:
            result = sync_mod._reconcile_incassi_task()
        finally:
            sync_mod._sync_lock.release()

        assert result["passes"] == 0
        assert result["marked_paid"] == 0
        assert result["partial"] is False
        assert "in corso" in result["message"].lower()
        assert called == [], "il rilevamento NON deve girare mentre un sync è in corso"

    def test_lock_released_after_run(self, monkeypatch, test_db_session):
        """Dopo un run normale il lock torna libero (nessuna perdita)."""
        _mk_invoice(test_db_session, "A/2026")
        _setup_fatturapro(monkeypatch, test_db_session, [_raw("A/2026")])
        sync_mod._reconcile_incassi_task()

        got = sync_mod._sync_lock.acquire(blocking=False)
        assert got, "il lock deve essere libero dopo il reconcile"
        sync_mod._sync_lock.release()

    def test_endpoint_returns_json_shape(self, monkeypatch, test_client):
        """L'endpoint HTTP restituisce le 4 chiavi del contratto."""
        monkeypatch.setattr(
            sync_mod, "_sync_invoices_task",
            lambda: {"fatturapro": {"partial": False, "paid_detected": 0}},
        )
        resp = test_client.post("/api/sync/reconcile-incassi")
        assert resp.status_code == 200
        body = resp.json()
        for key in ("passes", "marked_paid", "partial", "message"):
            assert key in body
        assert body["passes"] == 2


# ── FUNZIONE 2 — trasparenza per fattura ─────────────────────────────

class TestSerializerTrasparenza:
    def test_exposes_missing_streak_updated_paid(self, test_client, test_db_session):
        """Il serializer di /customers/{id} espone missing_streak, updated_at,
        paid_at su OGNI riga fattura."""
        c = _customer(test_db_session, "ACME SRL")
        _invoice(test_db_session, "OPEN/1", customer_id=c.id, status="open",
                 missing_streak=0)
        _invoice(test_db_session, "CONF/1", customer_id=c.id, status="open",
                 missing_streak=1)
        _invoice(test_db_session, "PAID/1", customer_id=c.id, status="paid",
                 amount_due=0.0, days_overdue=0,
                 paid_at=datetime(2026, 6, 1, 10, 0, 0),
                 amount_due_at_paid=100.0)

        resp = test_client.get(f"/api/customers/{c.id}")
        assert resp.status_code == 200
        items = {i["invoice_number"]: i for i in resp.json()["invoices"]["items"]}

        # Tutte le righe hanno i tre campi
        for inv in items.values():
            assert "missing_streak" in inv
            assert "updated_at" in inv
            assert "paid_at" in inv

        assert items["OPEN/1"]["missing_streak"] == 0
        assert items["OPEN/1"]["paid_at"] is None
        assert items["OPEN/1"]["updated_at"] is not None

        # "Conferma incasso in corso": sparita dalla lista FatturaPro
        assert items["CONF/1"]["missing_streak"] == 1

        # Pagata: paid_at valorizzato
        assert items["PAID/1"]["status"] == "paid"
        assert items["PAID/1"]["paid_at"] is not None
