"""Test dei fix acquisizione dati Shopify (batch 2026-07-16).

Il proprietario segnalava che telefono/email/numero d'ordine non
arrivavano mai dalla piattaforma. Cause confermate e coperte qui:
- paginazione ordini ROTTA: since_id su una prima pagina newest-first
  rileggeva gli stessi ordini recenti e non raggiungeva mai i vecchi
  → ora header Link/page_info come per i clienti
- criteri ordine→fattura troppo stretti: ±1% sul solo total_price
  (fatale con ordini ex-IVA) e finestra 30 giorni → subtotal_price
  accettato + finestra ORDER_MATCH_MAX_DAYS (90), near-miss nel result
- stesso ordine mai agganciato a due fatture nello stesso run
- P.IVA spazzatura da address2 ("Scala B - Interno 3") mai scritta sul
  cliente; una P.IVA valida esistente mai sovrascritta con spazzatura
- contatti Shopify copiati sull'orfano P.IVA-gemella (senza merge)
- import CSV Fattura24: numero fattura riusato da un ALTRO cliente
  (numerazione annuale) → nuova riga, mai update della fattura altrui
- /api/system: order_matching nel sync_info + Shopify 'unconfigured'
"""

import httpx
from datetime import date, datetime

from backend.connectors.shopify import ShopifyConnector
from backend.database import Invoice, Customer
from backend.config import config, Config

# P.IVA italiane checksum-valide (vedi backend/engine/piva.py)
PIVA_A = "12345678903"


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


def _order(oid, total, subtotal=None, created="2026-05-01"):
    return {
        "id": str(oid), "name": f"#SAK{oid}", "order_number": oid,
        "total_price": total,
        "subtotal_price": subtotal if subtotal is not None else total,
        "total_tax": 0.0,
        "created_at": f"{created}T00:00:00+02:00",
        "financial_status": "paid",
    }


# ── Paginazione ordini via header Link ───────────────────────────────

ORDERS_LINK_NEXT = (
    '<https://x.myshopify.com/admin/api/2026-01/orders.json'
    '?page_info={cursor}&limit=250>; rel="next"'
)


class TestOrderPagination:
    def _connector(self, monkeypatch):
        monkeypatch.setattr(config, "SHOPIFY_STORE_URL", "https://x.myshopify.com")
        monkeypatch.setattr(config, "SHOPIFY_ACCESS_TOKEN", "test-token")
        return ShopifyConnector()

    def test_newest_first_600_orders_fetched_once_each(self, monkeypatch):
        """Store con 600 ordini restituiti newest-first: con since_id se
        ne leggevano 499 (249 duplicati) e i vecchi mai; via header Link
        arrivano tutti e 600, una volta sola."""
        conn = self._connector(monkeypatch)
        all_orders = [
            {"id": i, "order_number": i, "name": f"#SAK{i}",
             "total_price": "122.00", "subtotal_price": "100.00",
             "total_tax": "22.00", "created_at": "2026-05-01T00:00:00+02:00",
             "financial_status": "paid"}
            for i in range(600, 0, -1)  # newest-first (created_at desc)
        ]
        pages = [all_orders[0:250], all_orders[250:500], all_orders[500:600]]
        calls = []

        def fake_get(endpoint, headers=None, params=None):
            calls.append(dict(params))
            idx = len(calls) - 1
            if idx < len(pages) - 1:
                conn.last_response_headers = httpx.Headers(
                    {"Link": ORDERS_LINK_NEXT.format(cursor=f"CUR{idx + 1}")}
                )
            else:
                conn.last_response_headers = httpx.Headers({})
            return {"orders": pages[idx]}

        monkeypatch.setattr(conn, "get", fake_get)
        monkeypatch.setattr(conn, "_get_headers", lambda: {})
        orders = conn.fetch_customer_orders("123")

        assert len(calls) == 3
        # Prima pagina: filtri espliciti, nessun cursore né since_id
        assert calls[0]["customer_id"] == "123"
        assert "page_info" not in calls[0]
        assert "since_id" not in calls[0]
        # Pagine successive: SOLO limit/fields/page_info (con page_info
        # Shopify ritiene i filtri originali e rifiuta di riceverli)
        assert calls[1]["page_info"] == "CUR1"
        assert calls[2]["page_info"] == "CUR2"
        for later in calls[1:]:
            assert "customer_id" not in later
            assert "created_at_min" not in later
            assert "since_id" not in later
        # Tutti e 600, nessun duplicato
        assert len(orders) == 600
        assert len({o["id"] for o in orders}) == 600
        # Gli importi ex-IVA arrivano al matching
        assert orders[0]["subtotal_price"] == 100.0
        assert orders[0]["total_tax"] == 22.0

    def test_single_page_stops_without_cursor(self, monkeypatch):
        conn = self._connector(monkeypatch)
        calls = []

        def fake_get(endpoint, headers=None, params=None):
            calls.append(dict(params))
            conn.last_response_headers = httpx.Headers({})
            return {"orders": [dict(_order(1, 100.0), id=1)]}

        monkeypatch.setattr(conn, "get", fake_get)
        monkeypatch.setattr(conn, "_get_headers", lambda: {})
        orders = conn.fetch_customer_orders("123")
        assert len(calls) == 1
        assert len(orders) == 1


# ── Criteri di match ordine→fattura ──────────────────────────────────

class TestOrderMatchCriteria:
    def test_ex_vat_order_matches_on_subtotal(self, test_db_session):
        # Ordine IVA inclusa (122) vs fattura pari all'imponibile (100):
        # il ±1% sul solo total_price scartava il match (delta ~22%).
        from backend.api.sync import _find_best_order_match
        inv = _mk_invoice(test_db_session, "EX/2026", amount=100.0,
                          issue_date=date(2026, 5, 10))
        best, _ = _find_best_order_match(
            inv, [_order(1, total=122.0, subtotal=100.0)]
        )
        assert best is not None and best["id"] == "1"

    def test_window_is_90_days(self, test_db_session):
        from backend.api.sync import _find_best_order_match
        inv = _mk_invoice(test_db_session, "W/2026", amount=100.0,
                          issue_date=date(2026, 7, 1))
        # 80 giorni: dentro la finestra (il vecchio limite era 30)
        best, _ = _find_best_order_match(
            inv, [_order(1, total=100.0, created="2026-04-12")]
        )
        assert best is not None
        # 122 giorni: fuori, e il candidato scartato emerge come near-miss
        best, near = _find_best_order_match(
            inv, [_order(2, total=100.0, created="2026-03-01")]
        )
        assert best is None
        assert near is not None and near["days"] > 90

    def test_near_miss_reports_best_discarded_candidate(self, test_db_session):
        from backend.api.sync import _find_best_order_match
        inv = _mk_invoice(test_db_session, "NM/2026", amount=100.0,
                          issue_date=date(2026, 5, 10))
        best, near = _find_best_order_match(
            inv, [_order(1, total=105.0, created="2026-05-08")]
        )
        assert best is None
        assert near == {"order": "#SAK1", "amount_delta": 5.0, "days": 2}

    def test_used_order_is_excluded(self, test_db_session):
        from backend.api.sync import _find_best_order_match
        inv = _mk_invoice(test_db_session, "U/2026", amount=100.0,
                          issue_date=date(2026, 5, 10))
        best, _ = _find_best_order_match(
            inv, [_order(1, total=100.0, created="2026-05-09")],
            used_order_ids={"1"},
        )
        assert best is None


class TestOrderMatchingTask:
    def _run(self, monkeypatch, session, orders):
        from backend.api import sync as sync_mod

        class FakeOrdersShopify:
            def __init__(self, *a, **kw):
                pass

            def fetch_customer_orders(self, shopify_id):
                return list(orders)

        monkeypatch.setattr(sync_mod, "ShopifyConnector", FakeOrdersShopify)
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: session)
        return sync_mod._match_orders_task()

    def test_same_order_never_linked_to_two_invoices(self, monkeypatch, test_db_session):
        cust = Customer(ragione_sociale="ACME SRL", shopify_id="555", source="shopify")
        test_db_session.add(cust)
        test_db_session.commit()
        _mk_invoice(test_db_session, "D1/2026", amount=100.0,
                    issue_date=date(2026, 5, 10), customer_id=cust.id)
        _mk_invoice(test_db_session, "D2/2026", amount=100.0,
                    issue_date=date(2026, 5, 12), customer_id=cust.id)

        result = self._run(
            monkeypatch, test_db_session,
            [_order(9, total=100.0, created="2026-05-09")],
        )

        assert result["matched"] == 1
        linked = test_db_session.query(Invoice).filter(
            Invoice.shopify_order_id.isnot(None)
        ).all()
        assert len(linked) == 1
        assert linked[0].invoice_number == "D1/2026"
        assert linked[0].shopify_order_number == "#SAK9"

    def test_order_already_linked_in_db_not_reused(self, monkeypatch, test_db_session):
        cust = Customer(ragione_sociale="ACME SRL", shopify_id="555", source="shopify")
        test_db_session.add(cust)
        test_db_session.commit()
        # L'ordine 9 è GIÀ agganciato a una fattura di un run precedente
        _mk_invoice(test_db_session, "OLD/2026", amount=100.0,
                    issue_date=date(2026, 5, 9), customer_id=cust.id,
                    shopify_order_id="9", shopify_order_number="#SAK9")
        _mk_invoice(test_db_session, "NEW/2026", amount=100.0,
                    issue_date=date(2026, 5, 10), customer_id=cust.id)

        result = self._run(
            monkeypatch, test_db_session,
            [_order(9, total=100.0, created="2026-05-09")],
        )

        assert result["matched"] == 0
        new = test_db_session.query(Invoice).filter_by(
            invoice_number="NEW/2026"
        ).one()
        assert new.shopify_order_id is None

    def test_near_misses_surface_in_result(self, monkeypatch, test_db_session):
        cust = Customer(ragione_sociale="ACME SRL", shopify_id="555", source="shopify")
        test_db_session.add(cust)
        test_db_session.commit()
        _mk_invoice(test_db_session, "NM2/2026", amount=100.0,
                    issue_date=date(2026, 5, 10), customer_id=cust.id)

        result = self._run(
            monkeypatch, test_db_session,
            [_order(3, total=110.0, created="2026-05-08")],
        )

        assert result["matched"] == 0
        assert result["near_misses"] == [{
            "invoice": "NM2/2026", "order": "#SAK3",
            "amount_delta": 10.0, "days": 2,
        }]


# ── P.IVA: mai scrivere spazzatura sul cliente ───────────────────────

class TestPivaWriteGuard:
    def _payload(self, piva):
        return {
            "shopify_id": "888", "ragione_sociale": "ACME SRL",
            "partita_iva": piva, "codice_fiscale": None, "codice_sdi": None,
            "phone": None, "phones": None, "email": None, "tags": "B2B",
        }

    def _run(self, monkeypatch, session, payload):
        from backend.api import sync as sync_mod

        class FakePivaShopify:
            def __init__(self, *a, **kw):
                pass

            def fetch_b2b_customers(self):
                return [payload]

        monkeypatch.setattr(sync_mod, "ShopifyConnector", FakePivaShopify)
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: session)
        monkeypatch.setattr(config, "SHOPIFY_ACCESS_TOKEN", "test-token")
        return sync_mod._sync_customers_task()

    def test_garbage_never_overwrites_valid_piva(self, monkeypatch, test_db_session):
        # 'Scala B - Interno 3' → parse_piva_from_address2 dà 'Scala B':
        # non deve toccare la P.IVA valida già in anagrafica.
        test_db_session.add(Customer(
            shopify_id="888", ragione_sociale="ACME SRL",
            partita_iva=PIVA_A, source="shopify",
        ))
        test_db_session.commit()

        result = self._run(monkeypatch, test_db_session, self._payload("Scala B"))

        assert result["success"] is True
        assert result["piva_discarded"] == 1
        row = test_db_session.query(Customer).filter_by(shopify_id="888").one()
        assert row.partita_iva == PIVA_A

    def test_valid_piva_corrects_stored_garbage(self, monkeypatch, test_db_session):
        test_db_session.add(Customer(
            shopify_id="888", ragione_sociale="ACME SRL",
            partita_iva="Scala B", source="shopify",
        ))
        test_db_session.commit()

        self._run(monkeypatch, test_db_session, self._payload(f"IT{PIVA_A}"))

        row = test_db_session.query(Customer).filter_by(shopify_id="888").one()
        assert row.partita_iva == PIVA_A  # normalizzata, senza prefisso IT

    def test_new_customer_created_without_garbage_piva(self, monkeypatch, test_db_session):
        result = self._run(monkeypatch, test_db_session, self._payload("Scala B"))
        assert result["created"] == 1
        row = test_db_session.query(Customer).filter_by(shopify_id="888").one()
        assert row.partita_iva is None


# ── Contatti copiati sull'orfano P.IVA-gemella (senza merge) ─────────

class TestOrphanContactCopy:
    def test_contacts_copied_where_missing_no_merge(self, monkeypatch, test_db_session):
        from backend.api import sync as sync_mod
        # Cliente Shopify GIÀ in DB + orfano nato dalle fatture con la
        # stessa P.IVA: il merge resta manuale, ma i contatti mancanti
        # vengono copiati sull'orfano (che porta le fatture da recuperare).
        shop = Customer(shopify_id="777", ragione_sociale="QOQA SRL",
                        partita_iva=PIVA_A, source="shopify")
        orphan = Customer(ragione_sociale="QOQA", partita_iva=PIVA_A,
                          email="pec@qoqa.it", source="fatturapro")
        test_db_session.add_all([shop, orphan])
        test_db_session.commit()
        orphan_id = orphan.id

        payload = {
            "shopify_id": "777", "ragione_sociale": "QOQA SRL",
            "partita_iva": PIVA_A, "codice_fiscale": None, "codice_sdi": None,
            "phone": "+39 333 000 1111",
            "phones": [{"number": "+39 333 000 1111",
                        "source": "shopify_customer", "label": "Shopify"}],
            "email": "shop@qoqa.it", "tags": "B2B",
        }

        class FakeTwinShopify:
            def __init__(self, *a, **kw):
                pass

            def fetch_b2b_customers(self):
                return [payload]

        monkeypatch.setattr(sync_mod, "ShopifyConnector", FakeTwinShopify)
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: test_db_session)
        monkeypatch.setattr(config, "SHOPIFY_ACCESS_TOKEN", "test-token")
        result = sync_mod._sync_customers_task()

        assert result["orphan_contacts_enriched"] == 1
        assert test_db_session.query(Customer).count() == 2  # nessun merge
        o = test_db_session.query(Customer).filter_by(id=orphan_id).one()
        assert o.shopify_id is None  # resta orfano
        assert o.phone == "+39 333 000 1111"
        assert o.phones_json[0]["number"] == "+39 333 000 1111"
        # L'email già presente NON viene sovrascritta: solo dove manca
        assert o.email == "pec@qoqa.it"


# ── Import CSV Fattura24: collisione numeri tra anni/clienti ─────────

class TestCsvImportCollision:
    def _import(self, test_client, csv_text):
        return test_client.post(
            "/api/sync/import-csv",
            files={"file": ("f24.csv", csv_text.encode("utf-8"), "text/csv")},
        )

    def _seed_domo_45(self, session):
        """Crea la '45' di Domò (2024) abbinata; ritorna l'id cliente."""
        cust = Customer(ragione_sociale="Domò SRL", source="fatture24")
        session.add(cust)
        session.commit()
        _mk_invoice(
            session, "45", source_platform="fatture24",
            customer_name_raw="Domò SRL", customer_id=cust.id,
            amount=500.0, amount_due=500.0, issue_date=date(2024, 3, 1),
        )
        return cust.id

    def test_same_number_other_customer_creates_new_row(
        self, monkeypatch, test_client, test_db_session
    ):
        # La numerazione italiana riparte ogni anno: la '45' di YOHO
        # (2025) NON deve sovrascrivere la '45' di Domò (2024) già
        # abbinata, cambiandole nome e importi senza ri-matcharla.
        from backend.api import sync as sync_mod
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: test_db_session)
        cust_id = self._seed_domo_45(test_db_session)

        resp = self._import(
            test_client,
            "Numero;Cliente;Importo;Data\n45;YOHO SRL;120,00;01/03/2025\n",
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] == 1
        assert body["updated"] == 0

        rows = test_db_session.query(Invoice).filter_by(invoice_number="45").all()
        assert len(rows) == 2
        domo = next(r for r in rows if r.customer_name_raw == "Domò SRL")
        assert domo.customer_id == cust_id  # abbinamento intatto
        assert domo.amount == 500.0

    def test_same_number_same_customer_still_updates(
        self, monkeypatch, test_client, test_db_session
    ):
        from backend.api import sync as sync_mod
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: test_db_session)
        self._seed_domo_45(test_db_session)

        resp = self._import(
            test_client,
            "Numero;Cliente;Importo;Data\n45;DOMO' S.R.L.;250,00;01/03/2024\n",
        )

        body = resp.json()
        assert body["updated"] == 1
        assert body["created"] == 0
        rows = test_db_session.query(Invoice).filter_by(invoice_number="45").all()
        assert len(rows) == 1
        assert rows[0].amount == 250.0


# ── /api/system: osservabilità order matching + Shopify ──────────────

def _empty_sync_status():
    return {
        key: {"last_sync": None, "result": None}
        for key in [
            "invoices", "customers", "matching",
            "auto_create", "order_matching", "cases",
        ]
    }


class TestSystemObservability:
    def _wire(self, monkeypatch, test_db_session, status):
        from backend.api import system as system_mod
        monkeypatch.setattr(system_mod, "get_session_direct", lambda: test_db_session)
        monkeypatch.setattr(system_mod, "_load_sync_state", lambda: None)
        monkeypatch.setattr(system_mod, "_sync_status", status)

    def test_order_matching_in_sync_info(self, monkeypatch, test_client, test_db_session):
        status = _empty_sync_status()
        status["order_matching"] = {
            "last_sync": datetime.utcnow().isoformat(),
            "result": {
                "matched": 3, "customers_processed": 5,
                "errors": ["ACME SRL: boom"],
                "near_misses": [{"invoice": "1/2026", "order": "#SAK1",
                                 "amount_delta": 4.5, "days": 12}],
            },
        }
        self._wire(monkeypatch, test_db_session, status)

        resp = test_client.get("/api/system")
        assert resp.status_code == 200
        info = resp.json()["sync"]["order_matching"]
        assert info["last_sync"] is not None
        assert "3 fatture agganciate" in info["result_summary"]
        assert "1 clienti in errore" in info["result_summary"]
        assert "1 near-miss" in info["result_summary"]

    def test_shopify_unconfigured_not_reported_ok(
        self, monkeypatch, test_client, test_db_session
    ):
        # Senza credenziali il task clienti "riesce" senza far nulla
        # (success=True): il connettore non deve risultare 'ok'.
        monkeypatch.setattr(Config, "SHOPIFY_ACCESS_TOKEN", "")
        monkeypatch.setattr(Config, "SHOPIFY_CLIENT_ID", "")
        monkeypatch.setattr(Config, "SHOPIFY_CLIENT_SECRET", "")
        status = _empty_sync_status()
        status["customers"] = {
            "last_sync": datetime.utcnow().isoformat(),
            "result": {"success": True, "created": 0, "updated": 0,
                       "unconfigured": True, "error": None},
        }
        self._wire(monkeypatch, test_db_session, status)

        resp = test_client.get("/api/system")
        body = resp.json()
        assert body["connectors"]["shopify"]["configured"] is False
        assert body["connectors"]["shopify"]["status"] == "unconfigured"
        assert body["sync"]["customers"]["result_summary"] == "Shopify non configurato"
