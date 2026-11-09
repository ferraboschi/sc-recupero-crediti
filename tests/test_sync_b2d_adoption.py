"""Sync clienti Shopify: distributori B2D / Wholesale e adozione per EMAIL.

Caso reale (9/11/2026): Cavo Luigi, Coloniale, Nove.10, Callmewine, Japlab…
esistono su Shopify con l'agente di vendita nei tag ma sono taggati B2D (non
B2B) e senza P.IVA in address2 → il sync non li importava e la scheda nata
dalle fatture restava senza collegamento, quindi senza agente.

Regole:
- il connettore chiede B2B + B2D + Wholesale;
- adozione per email solo se l'email è unica (orfani e lotto), le P.IVA non
  confliggono e i nomi si corrispondono (mai per solo nome);
- B2D/Wholesale senza scheda da agganciare: nessuna scheda nuova (contati);
- i B2B si creano come prima.
"""
import httpx
import pytest

from backend.api import sync as sync_mod
from backend.config import config
from backend.database import Customer

PIVA_A = "12345670785"


def _valid_piva(first10: str) -> str:
    total = 0
    for i, ch in enumerate(first10):
        d = int(ch)
        if i % 2 == 0:
            total += d
        else:
            dd = d * 2
            total += dd if dd < 10 else dd - 9
    return first10 + str((10 - (total % 10)) % 10)


PIVA_B = _valid_piva("9876543210")


def _orphan(session, nome, email, piva=PIVA_A, source="fatturapro"):
    """Crea la scheda e ritorna il suo ID: il task chiude la sessione, quindi
    dopo il sync la scheda va riletta con `_reload`."""
    c = Customer(ragione_sociale=nome, email=email, partita_iva=piva, source=source)
    session.add(c)
    session.commit()
    return c.id


def _reload(session, cid):
    session.expire_all()
    return session.get(Customer, cid)


def _shop(sid, nome, email, tags, piva=None):
    return {
        "shopify_id": f"gid://shopify/Customer/{sid}", "ragione_sociale": nome,
        "partita_iva": piva, "codice_fiscale": None, "codice_sdi": None,
        "phone": "+39 333 000 1111", "phones": [], "email": email, "tags": tags,
    }


def _run(monkeypatch, session, customers):
    class Fake:
        def __init__(self, *a, **k):
            pass

        def fetch_b2b_customers(self):
            return list(customers)

    monkeypatch.setattr(sync_mod, "ShopifyConnector", Fake)
    monkeypatch.setattr(sync_mod, "get_session_direct", lambda: session)
    monkeypatch.setattr(config, "SHOPIFY_ACCESS_TOKEN", "test-token")
    return sync_mod._sync_customers_task()


class TestConnectorQuery:
    def test_first_page_filters_all_business_tags(self, monkeypatch):
        from backend.connectors.shopify import ShopifyConnector
        monkeypatch.setattr(config, "SHOPIFY_STORE_URL", "test.myshopify.com")
        monkeypatch.setattr(config, "SHOPIFY_ACCESS_TOKEN", "test-token")
        conn = ShopifyConnector()
        calls = []

        def fake_get(endpoint, headers=None, params=None):
            calls.append(params)
            conn.last_response_headers = httpx.Headers({})
            return {"customers": []}

        monkeypatch.setattr(conn, "get", fake_get)
        monkeypatch.setattr(conn, "_get_headers", lambda: {})
        conn.fetch_b2b_customers()
        assert calls[0]["query"] == 'tag:"B2B" OR tag:"B2D" OR tag:"Wholesale"'


class TestEmailAdoption:
    def test_b2d_distributor_adopted_by_email_and_gets_agent(self, monkeypatch, test_db_session, test_client):
        orphan = _orphan(test_db_session, "Cavo Luigi Beverage Solutions srl", "geron.hanxhari@cavo.net", source="fatture24")
        res = _run(monkeypatch, test_db_session, [
            _shop(8209107747154, "Cavo Luigi Beverages Solutions", "Geron.Hanxhari@cavo.net", "B2D, Maggioni"),
        ])
        assert res["success"] and res["adopted_by_email"] == 1 and res["created"] == 0
        orphan = _reload(test_db_session, orphan)
        assert orphan.shopify_id == "gid://shopify/Customer/8209107747154"
        assert orphan.tags == "B2D, Maggioni"
        assert orphan.ragione_sociale == "Cavo Luigi Beverage Solutions srl"  # nome delle fatture conservato
        assert test_db_session.query(Customer).count() == 1
        detail = test_client.get(f"/api/customers/{orphan.id}").json()
        assert detail["agents"] == ["Maggioni"]

    def test_b2b_customer_also_adopted_by_email(self, monkeypatch, test_db_session):
        orphan = _orphan(test_db_session, "Zuma Rome Srl", "info@zuma.it", piva=None)
        res = _run(monkeypatch, test_db_session, [_shop(1, "Zuma Rome", "info@zuma.it", "B2B, Izzo")])
        assert res["adopted_by_email"] == 1 and res["created"] == 0
        orphan = _reload(test_db_session, orphan)
        assert orphan.shopify_id == "gid://shopify/Customer/1" and orphan.tags == "B2B, Izzo"

    def test_piva_adoption_still_wins_over_email(self, monkeypatch, test_db_session):
        orphan = _orphan(test_db_session, "QOQA", "qoqa@example.com", piva=PIVA_A)
        res = _run(monkeypatch, test_db_session, [_shop(2, "QOQA SRL", "altro@example.com", "B2D, Maggioni", piva=PIVA_A)])
        assert res["adopted"] == 1 and res["adopted_by_email"] == 0
        orphan = _reload(test_db_session, orphan)
        assert orphan.shopify_id == "gid://shopify/Customer/2"


class TestEmailAdoptionGuards:
    def test_email_shared_by_two_orphans_is_ambiguous(self, monkeypatch, test_db_session):
        _orphan(test_db_session, "Ristorante Kiyo", "studio@commercialista.it", piva=PIVA_A)
        _orphan(test_db_session, "Sushi Kiyo Due", "studio@commercialista.it", piva=PIVA_B)
        res = _run(monkeypatch, test_db_session, [_shop(3, "Ristorante Kiyo", "studio@commercialista.it", "B2D, Izzo")])
        assert res["adopted_by_email"] == 0 and res["skipped_non_b2b"] == 1
        assert all(c.shopify_id is None for c in test_db_session.query(Customer).all())

    def test_email_on_two_shopify_customers_is_ambiguous(self, monkeypatch, test_db_session):
        _orphan(test_db_session, "Ristorante Kiyo", "studio@commercialista.it")
        res = _run(monkeypatch, test_db_session, [
            _shop(4, "Ristorante Kiyo", "studio@commercialista.it", "B2D, Izzo"),
            _shop(5, "Kiyo Take Away", "studio@commercialista.it", "B2D, Izzo"),
        ])
        assert res["adopted_by_email"] == 0 and res["skipped_non_b2b"] == 2

    def test_piva_conflict_blocks_email_adoption(self, monkeypatch, test_db_session):
        _orphan(test_db_session, "Nove.10 Snc", "nove.10@gmail.com", piva=PIVA_A)
        res = _run(monkeypatch, test_db_session, [_shop(6, "Nove.10", "nove.10@gmail.com", "B2D, Maggioni", piva=PIVA_B)])
        assert res["adopted_by_email"] == 0 and res["skipped_non_b2b"] == 1

    def test_names_must_correspond(self, monkeypatch, test_db_session):
        orphan = _orphan(test_db_session, "Pizzeria Da Mario", "info@damario.it", piva=None)
        res = _run(monkeypatch, test_db_session, [_shop(7, "Trattoria Rossi", "info@damario.it", "B2D, Scotti")])
        assert res["adopted_by_email"] == 0 and res["skipped_non_b2b"] == 1
        orphan = _reload(test_db_session, orphan)
        assert orphan.shopify_id is None and orphan.tags is None

    def test_empty_shopify_name_relies_on_email_only(self, monkeypatch, test_db_session):
        orphan = _orphan(test_db_session, "Japlab srl", "info@japlab.it", piva=None)
        res = _run(monkeypatch, test_db_session, [_shop(8, "", "info@japlab.it", "B2D, Izzo")])
        assert res["adopted_by_email"] == 1
        orphan = _reload(test_db_session, orphan)
        assert orphan.ragione_sociale == "Japlab srl" and orphan.tags == "B2D, Izzo"


class TestNonB2bNeverCreatesCustomers:
    def test_b2d_without_match_is_counted_not_created(self, monkeypatch, test_db_session):
        res = _run(monkeypatch, test_db_session, [
            _shop(9, "Callmewine Srl", "acquisti@callmewine.com", "B2D, Maggioni"),
            _shop(10, "Tokyo Grill", "simone@tokyogrill.it", "Wholesale"),
        ])
        assert res["created"] == 0 and res["skipped_non_b2b"] == 2
        assert test_db_session.query(Customer).count() == 0

    def test_b2b_without_match_is_created_as_before(self, monkeypatch, test_db_session):
        res = _run(monkeypatch, test_db_session, [_shop(11, "Nuovo Ristorante", "nuovo@example.com", "B2B, Izzo")])
        assert res["created"] == 1 and res["skipped_non_b2b"] == 0
        c = test_db_session.query(Customer).one()
        assert c.shopify_id == "gid://shopify/Customer/11" and c.tags == "B2B, Izzo"

    def test_linked_b2d_customer_keeps_updating(self, monkeypatch, test_db_session):
        c = Customer(ragione_sociale="Coloniale Group", email="c@coloniale.it", shopify_id="gid://shopify/Customer/12", source="shopify", tags="B2D")
        test_db_session.add(c)
        test_db_session.commit()
        cid = c.id
        res = _run(monkeypatch, test_db_session, [_shop(12, "1492 Coloniale Group", "c@coloniale.it", "B2D, Maggioni")])
        assert res["updated"] == 1 and res["skipped_non_b2b"] == 0
        c = _reload(test_db_session, cid)
        assert c.tags == "B2D, Maggioni"
