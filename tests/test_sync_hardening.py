"""Test dei fix di sincronizzazione (batch 2026-07-15).

Copertura:
- parse_retry_after tollerante ("2.0" di Shopify non deve più uccidere il sync)
- paginazione Shopify via header Link (prima si fermava a 250 clienti)
- fetch_customer_orders rilancia invece di restituire liste parziali
- payment detection a DOPPIA assenza consecutiva (missing_streak)
- rotazione dell'enrichment dettaglio (mai tentate prima, poi le più vecchie)
- adozione per P.IVA dei clienti nati da FatturaPro nel sync clienti Shopify
- order matching: mai match per solo importo senza issue_date
- audit abbinamenti v2: P.IVA avvelenata, nome assente, include_paid
- matching: name_exact degradato a suggerimento se la P.IVA non è verificabile
- auto-create: merge name-only degradato a suggerimento
"""

import pytest
import httpx
from datetime import date, datetime, timedelta

from backend.connectors.base import parse_retry_after
from backend.connectors.shopify import ShopifyConnector
from backend.database import Invoice, Customer, SyncState
from backend.engine.matching import match_invoice_to_customer, piva_contradiction
from backend.config import config

# P.IVA italiane checksum-valide (vedi backend/engine/piva.py)
PIVA_A = "12345678903"
PIVA_B = "98765432103"
PIVA_C = "11111111115"


# ── parse_retry_after ────────────────────────────────────────────────

class TestParseRetryAfter:
    def test_float_string_from_shopify(self):
        assert parse_retry_after("2.0") == 2

    def test_plain_int_string(self):
        assert parse_retry_after("5") == 5

    def test_missing_header_defaults(self):
        assert parse_retry_after(None) == 2

    def test_garbage_defaults(self):
        assert parse_retry_after("Wed, 21 Oct 2026") == 2

    def test_capped(self):
        assert parse_retry_after("999999") == 60

    def test_floor_at_one_second(self):
        assert parse_retry_after("0") == 1
        assert parse_retry_after("-3") == 1


# ── Paginazione Shopify (header Link) ────────────────────────────────

LINK_NEXT = (
    '<https://x.myshopify.com/admin/api/2026-01/customers/search.json'
    '?page_info=CURSOR123&limit=250>; rel="next"'
)
LINK_PREV_ONLY = (
    '<https://x.myshopify.com/admin/api/2026-01/customers/search.json'
    '?page_info=BACK&limit=250>; rel="previous"'
)


class TestShopifyPagination:
    def _connector(self, monkeypatch):
        monkeypatch.setattr(config, "SHOPIFY_STORE_URL", "https://x.myshopify.com")
        monkeypatch.setattr(config, "SHOPIFY_ACCESS_TOKEN", "test-token")
        return ShopifyConnector()

    def test_cursor_from_link_header(self, monkeypatch):
        conn = self._connector(monkeypatch)
        conn.last_response_headers = httpx.Headers({"Link": LINK_NEXT})
        assert conn._extract_next_cursor() == "CURSOR123"

    def test_no_next_rel_means_done(self, monkeypatch):
        conn = self._connector(monkeypatch)
        conn.last_response_headers = httpx.Headers({"Link": LINK_PREV_ONLY})
        assert conn._extract_next_cursor() is None

    def test_no_headers_means_done(self, monkeypatch):
        conn = self._connector(monkeypatch)
        conn.last_response_headers = None
        assert conn._extract_next_cursor() is None

    def test_fetch_paginates_and_drops_query_param(self, monkeypatch):
        conn = self._connector(monkeypatch)
        calls = []

        def fake_get(endpoint, headers=None, params=None):
            calls.append(params)
            if len(calls) == 1:
                conn.last_response_headers = httpx.Headers({"Link": LINK_NEXT})
                return {"customers": [{"id": 1, "email": "a@x.it", "addresses": []}]}
            conn.last_response_headers = httpx.Headers({})
            return {"customers": [{"id": 2, "email": "b@x.it", "addresses": []}]}

        monkeypatch.setattr(conn, "get", fake_get)
        monkeypatch.setattr(conn, "_get_headers", lambda: {})
        customers = conn.fetch_b2b_customers()

        assert len(calls) == 2
        # Prima pagina: filtro query, nessun cursore
        assert "query" in calls[0] and "page_info" not in calls[0]
        # Seconda pagina: cursore, NIENTE query (Shopify la rifiuta)
        assert calls[1].get("page_info") == "CURSOR123"
        assert "query" not in calls[1]
        assert len(customers) == 2

    def test_customer_orders_error_propagates(self, monkeypatch):
        conn = self._connector(monkeypatch)

        def boom(*a, **kw):
            raise ValueError("invalid literal for int() with base 10: '2.0'")

        monkeypatch.setattr(conn, "get", boom)
        monkeypatch.setattr(conn, "_get_headers", lambda: {})
        with pytest.raises(ValueError):
            conn.fetch_customer_orders("123")


# ── Harness per i task di sync ───────────────────────────────────────

class FakeFatturaPro:
    """Connettore finto: lista fissa + scadenzario/anagrafica configurabili."""

    instances = []
    scadenze_map = {}
    clienti_map = {}
    scadenze_complete = True
    clienti_complete = True

    def __init__(self, *a, **kw):
        FakeFatturaPro.instances.append(self)

    def login(self):
        return True

    def fetch_overdue_invoices(self):
        return list(self.raw_invoices), False

    def fetch_scadenze_map(self, target_keys=None, max_pages=400, patience=20):
        return dict(FakeFatturaPro.scadenze_map), FakeFatturaPro.scadenze_complete

    def fetch_clienti_map(self):
        return dict(FakeFatturaPro.clienti_map), FakeFatturaPro.clienti_complete

    def close(self):
        pass


def _run_invoice_sync(monkeypatch, session, raw_invoices, scadenze=None, clienti=None,
                      scadenze_complete=True, clienti_complete=True):
    """Esegue _sync_invoices_task con connettore e sessione finti."""
    from backend.api import sync as sync_mod
    FakeFatturaPro.raw_invoices = raw_invoices
    FakeFatturaPro.instances = []
    FakeFatturaPro.scadenze_map = scadenze or {}
    FakeFatturaPro.clienti_map = clienti or {}
    FakeFatturaPro.scadenze_complete = scadenze_complete
    FakeFatturaPro.clienti_complete = clienti_complete
    monkeypatch.setattr(sync_mod, "FatturaProConnector", FakeFatturaPro)
    monkeypatch.setattr(sync_mod, "get_session_direct", lambda: session)
    return sync_mod._sync_invoices_task()


def _raw(number, doc_id="d1", balance=100.0, name="ACME SRL"):
    return {
        "invoice_number": number,
        "date": date(2026, 5, 1),
        "customer_name": name,
        "total": balance,
        "balance": balance,
        "doc_id": doc_id,
        "source_platform": "fatturapro",
    }


def _mk_invoice(session, number, **kw):
    inv = Invoice(
        invoice_number=number,
        amount=kw.pop("amount", 100.0),
        amount_due=kw.pop("amount_due", 100.0),
        issue_date=kw.pop("issue_date", date(2026, 4, 1)),
        source_platform=kw.pop("source_platform", "fatturapro"),
        status=kw.pop("status", "open"),
        **kw,
    )
    session.add(inv)
    session.commit()
    return inv


# ── Payment detection a doppia assenza ───────────────────────────────

class TestPaymentDetectionStreak:
    def test_first_absence_only_increments_streak(self, monkeypatch, test_db_session):
        _mk_invoice(test_db_session, "A/2026")
        _mk_invoice(test_db_session, "B/2026")

        # Fetch completo che contiene solo A: B è assente per la prima volta
        result = _run_invoice_sync(
            monkeypatch, test_db_session, [_raw("A/2026")]
        )
        assert result["fatturapro"]["paid_detected"] == 0
        b = test_db_session.query(Invoice).filter_by(invoice_number="B/2026").one()
        assert b.status == "open"
        assert b.missing_streak == 1

    def test_second_consecutive_absence_marks_paid(self, monkeypatch, test_db_session):
        _mk_invoice(test_db_session, "A/2026")
        _mk_invoice(test_db_session, "B/2026", missing_streak=1)

        result = _run_invoice_sync(
            monkeypatch, test_db_session, [_raw("A/2026")]
        )
        assert result["fatturapro"]["paid_detected"] == 1
        b = test_db_session.query(Invoice).filter_by(invoice_number="B/2026").one()
        assert b.status == "paid"
        assert b.amount_due == 0

    def test_reappearance_resets_streak(self, monkeypatch, test_db_session):
        _mk_invoice(test_db_session, "B/2026", missing_streak=1)

        _run_invoice_sync(monkeypatch, test_db_session, [_raw("B/2026")])
        b = test_db_session.query(Invoice).filter_by(invoice_number="B/2026").one()
        assert b.status == "open"
        assert b.missing_streak == 0


# ── Enrichment via scadenzario + anagrafica ──────────────────────────

class TestScadenzarioAnagraficaEnrichment:
    def test_real_due_date_joined_by_number(self, monkeypatch, test_db_session):
        # CUSTODE: la lista non ha scadenza; lo scadenzario la fornisce.
        result = _run_invoice_sync(
            monkeypatch, test_db_session,
            [_raw("2026/00001093/SAK - Fattura", name="Custode srl")],
            scadenze={"1093/SAK": date(2026, 7, 15)},
        )
        assert result["fatturapro"]["due_date_enriched"] == 1
        inv = test_db_session.query(Invoice).filter(
            Invoice.invoice_number.like("%1093%")
        ).one()
        assert inv.due_date == date(2026, 7, 15)
        assert inv.due_date_source == "real"

    def test_piva_joined_by_customer_name(self, monkeypatch, test_db_session):
        # La P.IVA arriva dall'anagrafica per nome — così Rooftop (IT) non
        # può assorbire una fattura QOQA (P.IVA diversa).
        _run_invoice_sync(
            monkeypatch, test_db_session,
            [_raw("993/SAK", name="Rooftop srl")],
            clienti={"rooftop srl": {"piva": "18148341003", "phone": None, "email": None}},
        )
        inv = test_db_session.query(Invoice).filter_by(invoice_number="993/SAK").one()
        assert inv.customer_piva_raw == "18148341003"

    def test_wrong_old_piva_overwritten_from_anagrafica(self, monkeypatch, test_db_session):
        # Una P.IVA vecchia sbagliata (venditore) viene corretta dall'anagrafica
        _mk_invoice(test_db_session, "500/SAK", customer_name_raw="Rooftop srl",
                    customer_piva_raw="10280600965")  # P.IVA venditore, sbagliata
        result = _run_invoice_sync(
            monkeypatch, test_db_session,
            [_raw("500/SAK", name="Rooftop srl")],
            clienti={"rooftop srl": {"piva": "18148341003", "phone": None, "email": None}},
        )
        assert result["fatturapro"]["piva_enriched"] == 1
        inv = test_db_session.query(Invoice).filter_by(invoice_number="500/SAK").one()
        assert inv.customer_piva_raw == "18148341003"

    def test_contacts_enriched_on_matching_customer(self, monkeypatch, test_db_session):
        cust = Customer(ragione_sociale="Rooftop srl", source="fatturapro")
        test_db_session.add(cust)
        test_db_session.commit()
        result = _run_invoice_sync(
            monkeypatch, test_db_session,
            [_raw("993/SAK", name="Rooftop srl")],
            clienti={"rooftop srl": {"piva": "18148341003", "phone": "0212345", "email": "info@rooftop.it"}},
        )
        assert result["fatturapro"]["contacts_enriched"] == 1
        c = test_db_session.query(Customer).filter_by(ragione_sociale="Rooftop srl").one()
        assert c.phone == "0212345"
        assert c.email == "info@rooftop.it"

    def test_soft_fail_when_lists_unavailable(self, monkeypatch, test_db_session):
        # Nessuno scadenzario/anagrafica: il sync procede comunque
        result = _run_invoice_sync(
            monkeypatch, test_db_session, [_raw("X/2026", name="ACME")],
        )
        assert result["fatturapro"]["success"] is True
        assert result["fatturapro"]["created"] == 1

    def test_proroga_updates_existing_real_due_date(self, monkeypatch, test_db_session):
        # due_date già 'real' 15/04; lo scadenzario ora dà una proroga 15/09
        _mk_invoice(test_db_session, "655/SAK", issue_date=date(2026, 3, 15),
                    due_date=date(2026, 4, 15), due_date_source="real")
        result = _run_invoice_sync(
            monkeypatch, test_db_session,
            [_raw("655/SAK", name="Belfiore")],
            scadenze={"655/SAK": date(2026, 9, 15)},
        )
        assert result["fatturapro"]["due_date_enriched"] == 1
        inv = test_db_session.query(Invoice).filter_by(invoice_number="655/SAK").one()
        assert inv.due_date == date(2026, 9, 15)  # la proroga si è propagata

    def test_partial_scadenzario_does_not_apply_due_dates(self, monkeypatch, test_db_session):
        # Scadenzario PARZIALE: non congelare scadenze 'real' potenzialmente
        # sbagliate (la rata più vecchia potrebbe mancare)
        result = _run_invoice_sync(
            monkeypatch, test_db_session,
            [_raw("A/2026", name="ACME")],
            scadenze={"A/2026": date(2026, 8, 1)},
            scadenze_complete=False,
        )
        assert result["fatturapro"]["scadenzario_ok"] is False
        inv = test_db_session.query(Invoice).filter_by(invoice_number="A/2026").one()
        assert inv.due_date_source != "real"

    def test_partial_anagrafica_does_not_apply_piva(self, monkeypatch, test_db_session):
        """Con anagrafica INCOMPLETA la P.IVA non va scritta.

        Il guard degli omonimi (clienti_map['ambiguous']) si calcola solo sulle
        righe scaricate: un'anagrafica troncata non rileva l'omonimo mai letto e
        attribuisce alla fattura la P.IVA dell'azienda sbagliata — che poi il
        matching per P.IVA aggancia in AUTOMATICO (non in quarantena).

        Gemello di test_partial_scadenzario_does_not_apply_due_dates.
        """
        # "Bar Roma" di Milano: l'omonimo di Roma sta nelle righe mai scaricate
        _mk_invoice(test_db_session, "B/2026", customer_name_raw="Bar Roma")
        result = _run_invoice_sync(
            monkeypatch, test_db_session,
            [_raw("B/2026", name="Bar Roma")],
            clienti={"bar roma": {"piva": PIVA_A, "phone": None, "email": None}},
            clienti_complete=False,
        )
        assert result["fatturapro"]["anagrafica_ok"] is False
        assert result["fatturapro"]["piva_enriched"] == 0
        inv = test_db_session.query(Invoice).filter_by(invoice_number="B/2026").one()
        assert inv.customer_piva_raw is None

    def test_complete_anagrafica_applies_piva(self, monkeypatch, test_db_session):
        # Direzione opposta del gemello sopra: identico, ma anagrafica COMPLETA
        # → l'omonimo sarebbe stato rilevato, quindi la P.IVA si applica.
        _mk_invoice(test_db_session, "B/2026", customer_name_raw="Bar Roma")
        result = _run_invoice_sync(
            monkeypatch, test_db_session,
            [_raw("B/2026", name="Bar Roma")],
            clienti={"bar roma": {"piva": PIVA_A, "phone": None, "email": None}},
            clienti_complete=True,
        )
        assert result["fatturapro"]["anagrafica_ok"] is True
        assert result["fatturapro"]["piva_enriched"] == 1
        inv = test_db_session.query(Invoice).filter_by(invoice_number="B/2026").one()
        assert inv.customer_piva_raw == PIVA_A

    def test_completeness_flags_in_result(self, monkeypatch, test_db_session):
        result = _run_invoice_sync(
            monkeypatch, test_db_session, [_raw("A/2026", name="ACME")],
            scadenze={"A/2026": date(2026, 8, 1)},
            clienti={"acme": {"piva": PIVA_A, "phone": None, "email": None}},
            clienti_complete=False,
        )
        assert result["fatturapro"]["scadenzario_ok"] is True
        assert result["fatturapro"]["anagrafica_ok"] is False


# ── Adozione clienti per P.IVA nel sync Shopify ──────────────────────

class FakeShopify:
    def __init__(self, *a, **kw):
        pass

    def fetch_b2b_customers(self):
        return [{
            "shopify_id": "gid://shopify/Customer/777",
            "ragione_sociale": "QOQA SRL",
            "partita_iva": PIVA_A,
            "codice_fiscale": None,
            "codice_sdi": None,
            "phone": "+39 333 000 1111",
            "phones": [{"number": "+39 333 000 1111", "source": "shopify"}],
            "email": "qoqa@example.com",
            "tags": "B2B",
        }]


class TestCustomerAdoption:
    def _run(self, monkeypatch, session):
        from backend.api import sync as sync_mod
        monkeypatch.setattr(sync_mod, "ShopifyConnector", FakeShopify)
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: session)
        monkeypatch.setattr(config, "SHOPIFY_ACCESS_TOKEN", "test-token")
        return sync_mod._sync_customers_task()

    def test_orphan_adopted_instead_of_duplicated(self, monkeypatch, test_db_session):
        orphan = Customer(
            ragione_sociale="QOQA",
            partita_iva=f"IT{PIVA_A}",  # con prefisso: il confronto normalizza
            source="fatturapro",
        )
        test_db_session.add(orphan)
        test_db_session.commit()

        result = self._run(monkeypatch, test_db_session)

        assert result["adopted"] == 1
        assert result["created"] == 0
        rows = test_db_session.query(Customer).all()
        assert len(rows) == 1
        assert rows[0].shopify_id == "gid://shopify/Customer/777"
        assert rows[0].email == "qoqa@example.com"
        assert rows[0].phone == "+39 333 000 1111"
        # Il nome derivato dalle fatture NON viene sovrascritto da Shopify:
        # è quello su cui lavora il matching per nome.
        assert rows[0].ragione_sociale == "QOQA"

    def test_empty_shopify_name_never_overwrites(self, monkeypatch, test_db_session):
        """Un profilo Shopify senza company produce ragione_sociale="":
        non deve azzerare il nome buono già in anagrafica."""
        existing = Customer(
            shopify_id="gid://shopify/Customer/777",
            ragione_sociale="QOQA SRL",
            ragione_sociale_normalized="qoqa",
            source="shopify",
        )
        test_db_session.add(existing)
        test_db_session.commit()

        def empty_name_fetch(self):
            return [{
                "shopify_id": "gid://shopify/Customer/777",
                "ragione_sociale": "",
                "partita_iva": None, "codice_fiscale": None, "codice_sdi": None,
                "phone": None, "phones": None, "email": "x@example.com", "tags": "B2B",
            }]

        monkeypatch.setattr(FakeShopify, "fetch_b2b_customers", empty_name_fetch)
        self._run(monkeypatch, test_db_session)

        # re-query: il task chiude la sessione e stacca le istanze
        row = test_db_session.query(Customer).filter_by(
            shopify_id="gid://shopify/Customer/777"
        ).one()
        assert row.ragione_sociale == "QOQA SRL"
        assert row.ragione_sociale_normalized == "qoqa"
        assert row.email == "x@example.com"

    def test_new_customer_still_created_when_no_orphan(self, monkeypatch, test_db_session):
        result = self._run(monkeypatch, test_db_session)
        assert result["created"] == 1
        assert result.get("adopted", 0) == 0


# ── Order matching ───────────────────────────────────────────────────

class TestOrderMatching:
    def test_no_issue_date_no_match(self, test_db_session):
        from backend.api.sync import _find_best_order_match
        inv = _mk_invoice(test_db_session, "X/2026", issue_date=None)
        orders = [{
            "id": "1", "name": "#SAK1", "order_number": 1,
            "total_price": 100.0, "created_at": "2026-05-01T00:00:00",
            "financial_status": "paid",
        }]
        best, near = _find_best_order_match(inv, orders)
        assert best is None and near is None

    def test_best_match_prefers_closest(self, test_db_session):
        from backend.api.sync import _find_best_order_match
        inv = _mk_invoice(test_db_session, "Y/2026", issue_date=date(2026, 5, 10))
        orders = [
            {"id": "1", "name": "#SAK1", "order_number": 1,
             "total_price": 100.0, "created_at": "2026-04-15T00:00:00",
             "financial_status": "paid"},
            {"id": "2", "name": "#SAK2", "order_number": 2,
             "total_price": 100.0, "created_at": "2026-05-09T00:00:00",
             "financial_status": "paid"},
        ]
        best, _ = _find_best_order_match(inv, orders)
        assert best["id"] == "2"


# ── Audit abbinamenti v2 ─────────────────────────────────────────────

class TestMatchAuditV2:
    @pytest.fixture(autouse=True)
    def _use_test_session(self, monkeypatch, test_db_session):
        # L'endpoint audit apre la sessione via get_session_direct, non via
        # dependency injection: va reindirizzato alla sessione di test.
        from backend.api import system as system_mod
        monkeypatch.setattr(system_mod, "get_session_direct", lambda: test_db_session)

    def _customer(self, session, name, piva=None):
        c = Customer(ragione_sociale=name, partita_iva=piva, source="shopify")
        session.add(c)
        session.commit()
        return c

    def test_poisoned_piva_flagged_bad(self, test_client, test_db_session):
        # P.IVA coincidente ma nomi completamente diversi: il vecchio motore
        # aveva scritto la P.IVA della fattura sul cliente sbagliato.
        cust = self._customer(test_db_session, "Rooftop SRL", PIVA_A)
        _mk_invoice(
            test_db_session, "993/2026", customer_id=cust.id,
            customer_name_raw="QOQA di Amanda Piccolo", customer_piva_raw=PIVA_A,
            match_method="legacy", days_overdue=10,
        )
        resp = test_client.get("/api/system/match-audit")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["verdict"] == "bad"
        assert "avvelenata" in items[0]["reasons"][0]
        assert items[0]["verification"]["level"] == "critical"

    def test_missing_raw_name_is_warn_not_ok(self, test_client, test_db_session):
        cust = self._customer(test_db_session, "Rooftop SRL")
        _mk_invoice(
            test_db_session, "994/2026", customer_id=cust.id,
            customer_name_raw=None, match_method="legacy", days_overdue=10,
        )
        resp = test_client.get("/api/system/match-audit")
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["verdict"] == "warn"

    def test_include_paid_covers_paid_invoices(self, test_client, test_db_session):
        cust = self._customer(test_db_session, "Belfiore M & M srl", PIVA_A)
        _mk_invoice(
            test_db_session, "655/2026", customer_id=cust.id, status="paid",
            customer_name_raw="Altra Azienda SRL", customer_piva_raw=PIVA_B,
            match_method="legacy",
        )
        # Senza include_paid la pagata è invisibile
        resp = test_client.get("/api/system/match-audit")
        assert resp.json()["total_audited"] == 0
        # Con include_paid emerge il conflitto P.IVA
        resp = test_client.get("/api/system/match-audit?include_paid=true")
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["verdict"] == "bad"

    def test_include_paid_denominator_coherent(self, test_client, test_db_session):
        # Con include_paid il total_invoices del gruppo deve coprire ANCHE le
        # pagate: altrimenti problem_count (che le include) supera il
        # denominatore → "2 fatture su 0". Cliente con 2 PAGATE problematiche.
        cust = self._customer(test_db_session, "Ferro Distribuzione SRL", PIVA_A)
        for n in ("100/2026", "101/2026"):
            _mk_invoice(
                test_db_session, n, customer_id=cust.id, status="paid",
                customer_name_raw="Altra Azienda SRL", customer_piva_raw=PIVA_B,
                match_method="legacy",
            )
        resp = test_client.get("/api/system/match-audit?include_paid=true")
        groups = resp.json()["groups"]
        assert len(groups) == 1
        g = groups[0]
        assert g["problem_count"] == 2
        assert g["total_invoices"] == 2      # coerente: mai "2 su 0"
        assert g["problem_count"] <= g["total_invoices"]

    def test_grouped_by_customer_totals(self, test_client, test_db_session):
        # Un cliente con 3 fatture non-pagate: 2 problematiche (P.IVA in
        # conflitto) + 1 ok. Il gruppo deve dire "2 problemi su 3 totali".
        cust = self._customer(test_db_session, "Ferro Distribuzione SRL", PIVA_A)
        _mk_invoice(
            test_db_session, "F1/2026", customer_id=cust.id,
            customer_name_raw="Ferro Distribuzione SRL", customer_piva_raw=PIVA_B,
        )
        _mk_invoice(
            test_db_session, "F2/2026", customer_id=cust.id,
            customer_name_raw="Ferro Distribuzione SRL", customer_piva_raw=PIVA_B,
        )
        _mk_invoice(
            test_db_session, "F3/2026", customer_id=cust.id,
            customer_name_raw="Ferro Distribuzione SRL", customer_piva_raw=PIVA_A,
        )
        data = test_client.get("/api/system/match-audit").json()
        assert len(data["groups"]) == 1
        g = data["groups"][0]
        assert g["customer_id"] == cust.id
        assert g["total_invoices"] == 3
        assert g["problem_count"] == 2
        assert len(g["items"]) == 2
        assert g["worst_verdict"] == "bad"
        assert g["problems_amount_due"] == 200.0
        # Il cliente HA già una P.IVA valida (diversa): non si può assegnare.
        assert g["items"][0]["can_assign_piva"] is False

    def test_can_assign_piva_when_invoice_has_piva_and_customer_none(
        self, test_client, test_db_session
    ):
        # Fattura con P.IVA valida, cliente SENZA P.IVA: caso "copia la P.IVA".
        cust = self._customer(test_db_session, "Rossi SRL", None)
        _mk_invoice(
            test_db_session, "R1/2026", customer_id=cust.id,
            customer_name_raw="Rossi SRL", customer_piva_raw=PIVA_A,
        )
        data = test_client.get("/api/system/match-audit").json()
        item = data["groups"][0]["items"][0]
        assert item["verdict"] == "warn"
        assert item["can_assign_piva"] is True

    def test_reviewed_invoice_excluded_then_included(self, test_client, test_db_session):
        cust = self._customer(test_db_session, "Rossi SRL", None)
        inv = _mk_invoice(
            test_db_session, "R2/2026", customer_id=cust.id,
            customer_name_raw="Rossi SRL", customer_piva_raw=PIVA_A,
        )
        # Di default è un problema visibile.
        data = test_client.get("/api/system/match-audit").json()
        assert data["total_problems"] == 1
        assert data["reviewed_count"] == 0
        assert len(data["groups"]) == 1
        # Segna verificato → sparisce dai problemi, ma reviewed_count sale.
        r = test_client.post(f"/api/positions/{inv.id}/mark-reviewed")
        assert r.status_code == 200
        data = test_client.get("/api/system/match-audit").json()
        assert data["reviewed_count"] == 1
        assert len(data["groups"]) == 0
        # total_problems resta invoice-level (invariato).
        assert data["total_problems"] == 1
        # Con include_reviewed ricompare, marcata come verificata.
        data = test_client.get(
            "/api/system/match-audit?include_reviewed=true"
        ).json()
        assert len(data["groups"]) == 1
        assert data["groups"][0]["items"][0]["reviewed"] is True


# ── Azioni dell'audit: assegna P.IVA, segna/annulla verificato ───────

class TestAuditActions:
    def test_assign_piva_happy_path(self, test_client, test_db_session):
        cust = Customer(ragione_sociale="Rossi SRL", partita_iva=None, source="manual")
        test_db_session.add(cust)
        test_db_session.commit()
        inv = _mk_invoice(
            test_db_session, "AP1/2026", customer_id=cust.id, customer_piva_raw=PIVA_A,
        )
        r = test_client.post(f"/api/positions/{inv.id}/assign-piva-to-customer")
        assert r.status_code == 200
        assert r.json()["partita_iva"] == PIVA_A
        test_db_session.refresh(cust)
        assert cust.partita_iva == PIVA_A

    def test_assign_piva_noop_when_same_normalized(self, test_client, test_db_session):
        cust = Customer(ragione_sociale="Rossi SRL", partita_iva=PIVA_A, source="manual")
        test_db_session.add(cust)
        test_db_session.commit()
        inv = _mk_invoice(
            test_db_session, "AP4/2026", customer_id=cust.id,
            customer_piva_raw=f"IT{PIVA_A}",
        )
        r = test_client.post(f"/api/positions/{inv.id}/assign-piva-to-customer")
        assert r.status_code == 200

    def test_assign_piva_409_customer_has_different_valid(
        self, test_client, test_db_session
    ):
        cust = Customer(ragione_sociale="Rossi SRL", partita_iva=PIVA_B, source="manual")
        test_db_session.add(cust)
        test_db_session.commit()
        inv = _mk_invoice(
            test_db_session, "AP2/2026", customer_id=cust.id, customer_piva_raw=PIVA_A,
        )
        r = test_client.post(f"/api/positions/{inv.id}/assign-piva-to-customer")
        assert r.status_code == 409
        # Non deve aver sovrascritto.
        test_db_session.refresh(cust)
        assert cust.partita_iva == PIVA_B

    def test_assign_piva_400_invoice_without_valid_piva(
        self, test_client, test_db_session
    ):
        cust = Customer(ragione_sociale="Rossi SRL", partita_iva=None, source="manual")
        test_db_session.add(cust)
        test_db_session.commit()
        inv = _mk_invoice(
            test_db_session, "AP3/2026", customer_id=cust.id,
            customer_piva_raw="12345678901",  # checksum invalido = assente
        )
        r = test_client.post(f"/api/positions/{inv.id}/assign-piva-to-customer")
        assert r.status_code == 400

    def test_mark_and_unmark_reviewed_toggle_field(self, test_client, test_db_session):
        inv = _mk_invoice(test_db_session, "MR1/2026")
        r = test_client.post(f"/api/positions/{inv.id}/mark-reviewed")
        assert r.status_code == 200
        test_db_session.refresh(inv)
        assert inv.audit_reviewed_at is not None
        r = test_client.post(f"/api/positions/{inv.id}/unmark-reviewed")
        assert r.status_code == 200
        test_db_session.refresh(inv)
        assert inv.audit_reviewed_at is None


# ── Reassign: confronto P.IVA normalizzato ma NON validato ───────────

class TestReassignPivaGuard:
    def test_it_prefix_same_piva_no_false_409(self, test_client, test_db_session):
        cust = Customer(ragione_sociale="QOQA SRL", partita_iva=PIVA_A, source="shopify")
        test_db_session.add(cust)
        test_db_session.commit()
        inv = _mk_invoice(
            test_db_session, "RA/2026", customer_piva_raw=f"IT{PIVA_A}",
        )
        resp = test_client.put(
            f"/api/positions/{inv.id}/reassign?new_customer_id={cust.id}"
        )
        assert resp.status_code == 200

    def test_malformed_piva_still_blocks(self, test_client, test_db_session):
        """Una P.IVA presente ma checksum-invalida NON deve bypassare il
        blocco: è un guard di sicurezza manuale, non un match automatico."""
        cust = Customer(ragione_sociale="Altro SRL", partita_iva=PIVA_B, source="shopify")
        test_db_session.add(cust)
        test_db_session.commit()
        inv = _mk_invoice(
            test_db_session, "RB/2026", customer_piva_raw="12345678901",  # invalida
        )
        resp = test_client.put(
            f"/api/positions/{inv.id}/reassign?new_customer_id={cust.id}"
        )
        assert resp.status_code == 409


# ── Matching: name_exact con P.IVA non verificabile ──────────────────

class TestNameExactUnverified:
    def test_degraded_to_suggestion(self, test_db_session):
        cust = Customer(ragione_sociale="QOQA SRL", partita_iva=None, source="manual")
        test_db_session.add(cust)
        test_db_session.commit()
        inv = _mk_invoice(
            test_db_session, "Z/2026",
            customer_name_raw="QOQA SRL", customer_piva_raw=PIVA_A,
        )
        result = match_invoice_to_customer(inv, [cust], test_db_session)
        assert result.customer is None
        assert result.suggested_customer is cust
        assert result.suggested_method == "name_exact_piva_unverified"

    def test_still_automatic_when_invoice_has_no_piva(self, test_db_session):
        cust = Customer(ragione_sociale="QOQA SRL", partita_iva=None, source="manual")
        test_db_session.add(cust)
        test_db_session.commit()
        inv = _mk_invoice(
            test_db_session, "Z2/2026", customer_name_raw="QOQA SRL",
        )
        result = match_invoice_to_customer(inv, [cust], test_db_session)
        assert result.customer is cust
        assert result.method == "name_exact"


# ── piva_contradiction ───────────────────────────────────────────────

class TestPivaContradiction:
    def test_true_only_when_both_valid_and_different(self, test_db_session):
        cust_a = Customer(ragione_sociale="A", partita_iva=PIVA_A, source="manual")
        cust_none = Customer(ragione_sociale="N", partita_iva=None, source="manual")
        test_db_session.add_all([cust_a, cust_none])
        test_db_session.commit()
        inv_b = _mk_invoice(test_db_session, "K/2026", customer_piva_raw=PIVA_B)
        inv_a = _mk_invoice(test_db_session, "K2/2026", customer_piva_raw=f"IT {PIVA_A}")
        inv_invalid = _mk_invoice(test_db_session, "K3/2026", customer_piva_raw="12345678901")

        assert piva_contradiction(inv_b, cust_a) is True
        assert piva_contradiction(inv_a, cust_a) is False      # stessa, normalizzata
        assert piva_contradiction(inv_invalid, cust_a) is False  # invalida = assente
        assert piva_contradiction(inv_b, cust_none) is False
        assert piva_contradiction(inv_b, None) is False


# ── Auto-create: merge name-only degradato ───────────────────────────

class TestAutoCreateNameOnlyMerge:
    def _run(self, monkeypatch, session):
        from backend.api import sync as sync_mod
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: session)
        return sync_mod._auto_create_task()

    def test_same_normalized_name_no_piva_degrades_to_suggestion(
        self, monkeypatch, test_db_session
    ):
        _mk_invoice(
            test_db_session, "T1/2026",
            customer_name_raw="Trattoria X di Mario Rossi SNC",
        )
        _mk_invoice(
            test_db_session, "T2/2026",
            customer_name_raw="Trattoria X di Luigi Bianchi SRL",
        )
        result = self._run(monkeypatch, test_db_session)

        assert result["auto_created"] == 1
        assert result["matched_within_run"] == 0
        t2 = test_db_session.query(Invoice).filter_by(invoice_number="T2/2026").one()
        assert t2.customer_id is None
        assert t2.suggested_customer_id is not None
        assert t2.suggested_method == "name_ambiguous"

    def test_same_piva_still_attaches(self, monkeypatch, test_db_session):
        _mk_invoice(
            test_db_session, "P1/2026",
            customer_name_raw="QOQA SRL", customer_piva_raw=PIVA_A,
        )
        _mk_invoice(
            test_db_session, "P2/2026",
            customer_name_raw="QOQA S.R.L.", customer_piva_raw=PIVA_A,
        )
        result = self._run(monkeypatch, test_db_session)
        assert result["auto_created"] == 1
        assert result["matched_within_run"] == 1
