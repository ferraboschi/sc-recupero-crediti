"""Test del layer 'Forse intendevi' (ricerca approssimata clienti).

- rank_similar: ranking fuzzy su chiavi normalizzate (accenti/forme
  legali/punteggiatura ignorati).
- GET /api/customers/suggest: endpoint dei suggerimenti.
"""

from datetime import date

from backend.database import Customer, Invoice
from backend.engine.normalizer import rank_similar


class TestRankSimilar:
    NAMES = [
        "Domò Milano", "Yoho Milano", "Sakeya S.r.l.",
        "Rooftop S.R.L.", "Osteria del Borgo",
    ]

    def _top(self, q):
        return [(self.NAMES[i], s) for i, s in rank_similar(q, self.NAMES)]

    def test_accents_collapse(self):
        # "Domo"/"Domó"/"Domò" sono la stessa ragione sociale.
        for q in ("Domo Milano", "Domó Milano", "Domò Milano"):
            top = self._top(q)
            assert top and top[0][0] == "Domò Milano"
            assert top[0][1] == 100

    def test_legal_form_and_partial(self):
        for q in ("Sakeya Srl", "Sakeya S.r.l.", "Sakeya"):
            top = self._top(q)
            assert top and top[0][0] == "Sakeya S.r.l."

    def test_typo_tolerated(self):
        top = self._top("Sakya")
        assert top and top[0][0] == "Sakeya S.r.l."
        assert top[0][1] >= 80

    def test_shared_token_ranks_below_real_variant(self):
        # "Yoho Milano" condivide 'milano' con "Domo Milano" ma NON è la
        # stessa azienda: deve restare sotto la variante reale.
        top = self._top("Domo Milano")
        assert top[0][0] == "Domò Milano"
        assert top[0][1] > (dict(self._top("Domo Milano")).get("Yoho Milano", 0))

    def test_no_match_returns_empty(self):
        assert rank_similar("azienda inesistente xyz", self.NAMES) == []

    def test_empty_query(self):
        assert rank_similar("", self.NAMES) == []


class TestSuggestEndpoint:
    def _seed(self, session):
        for name, piva in [
            ("Domò Milano", None),
            ("Sakeya S.r.l.", "12345678903"),
            ("Rooftop S.R.L.", "98765432103"),
        ]:
            session.add(Customer(ragione_sociale=name, partita_iva=piva, source="shopify"))
        session.commit()
        # una scaduta su Domò Milano
        domo = session.query(Customer).filter_by(ragione_sociale="Domò Milano").one()
        session.add(Invoice(
            invoice_number="F1", amount=100.0, amount_due=100.0,
            issue_date=date(2026, 4, 1), due_date=date(2026, 5, 1),
            days_overdue=20, status="open", customer_id=domo.id,
            source_platform="fatturapro",
        ))
        session.commit()
        return domo

    def test_suggest_finds_accented_name(self, test_client, test_db_session):
        domo = self._seed(test_db_session)
        resp = test_client.get("/api/customers/suggest", params={"q": "Domo Milano"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items[0]["id"] == domo.id
        assert items[0]["ragione_sociale"] == "Domò Milano"
        assert items[0]["score"] == 100
        assert items[0]["overdue_count"] == 1  # la scaduta è riportata

    def test_suggest_legal_form_variant(self, test_client, test_db_session):
        self._seed(test_db_session)
        resp = test_client.get("/api/customers/suggest", params={"q": "Sakeya Srl"})
        items = resp.json()["items"]
        assert any(i["ragione_sociale"] == "Sakeya S.r.l." for i in items)

    def test_suggest_no_match_empty(self, test_client, test_db_session):
        self._seed(test_db_session)
        resp = test_client.get("/api/customers/suggest", params={"q": "zzzz qwerty"})
        assert resp.json()["items"] == []

    def test_suggest_min_length(self, test_client, test_db_session):
        self._seed(test_db_session)
        resp = test_client.get("/api/customers/suggest", params={"q": "a"})
        assert resp.status_code == 422  # min_length=2

    def test_suggest_not_shadowed_by_customer_id_route(self, test_client, test_db_session):
        # 'suggest' non deve essere interpretato come /{customer_id}.
        self._seed(test_db_session)
        resp = test_client.get("/api/customers/suggest", params={"q": "Rooftop"})
        assert resp.status_code == 200
        assert any(i["ragione_sociale"] == "Rooftop S.R.L." for i in resp.json()["items"])
