"""Test visibilità della quarantena e creazione cliente da fattura.

Copre i buchi di visibilità del caso Belfiore/YOHO:
- il profilo cliente espone le fatture in quarantena suggerite a lui
  (pending_suggestions), INCLUSE le pagate;
- POST /positions/{id}/create-customer crea il cliente giusto quando il
  suggerimento è sbagliato (guardie 400/409);
- il badge low_confidence copre anche gli score bassissimi non-fuzzy.
"""

from datetime import date, timedelta

import pytest

from backend.database import Customer, Invoice, ActivityLog

# P.IVA con checksum VALIDO (l'ultima cifra 3 è il check di '1234567890');
# '12345678901' delle altre fixture NON passa validate_piva.
VALID_PIVA = "12345678903"


@pytest.fixture
def quarantined_invoice(test_db_session, sample_customer):
    """Fattura in quarantena: nessun cliente, suggerimento verso sample_customer."""
    today = date.today()
    inv = Invoice(
        invoice_number="655/2026",
        amount=480.0,
        amount_due=480.0,
        issue_date=today - timedelta(days=60),
        due_date=today - timedelta(days=30),
        days_overdue=30,
        status="open",
        customer_id=None,
        suggested_customer_id=sample_customer.id,
        suggested_method="fuzzy",
        suggested_score=78,
        customer_name_raw="Belfiore Ristorante",
        customer_piva_raw=None,
        source_platform="fatturapro",
    )
    test_db_session.add(inv)
    test_db_session.commit()
    return inv


class TestCustomerDetailPendingSuggestions:
    """Il profilo cliente deve mostrare le fatture in quarantena suggerite a lui."""

    def test_pending_suggestion_visible_on_profile(
        self, test_client, test_db_session, sample_customer, quarantined_invoice
    ):
        """Caso Belfiore: la quarantenata compare in pending_suggestions."""
        response = test_client.get(f"/api/customers/{sample_customer.id}")
        assert response.status_code == 200
        data = response.json()

        assert "pending_suggestions" in data
        assert len(data["pending_suggestions"]) == 1
        sug = data["pending_suggestions"][0]
        assert sug["id"] == quarantined_invoice.id
        assert sug["invoice_number"] == "655/2026"
        assert sug["amount_due"] == 480.0
        assert sug["status"] == "open"
        assert sug["days_overdue"] == 30
        assert sug["suggested_method"] == "fuzzy"
        assert sug["suggested_score"] == 78
        assert sug["customer_name_raw"] == "Belfiore Ristorante"
        assert sug["source_platform"] == "fatturapro"
        assert sug["due_date"] is not None
        assert sug["issue_date"] is not None
        # Non deve comparire tra le fatture abbinate
        assert all(i["id"] != quarantined_invoice.id for i in data["invoices"]["items"])

    def test_paid_pending_suggestion_included(
        self, test_client, test_db_session, sample_customer
    ):
        """Una quarantenata PAGATA resta visibile qui (altrove è filtrata)."""
        inv = Invoice(
            invoice_number="656/2026",
            amount=100.0,
            amount_due=0.0,
            status="paid",
            customer_id=None,
            suggested_customer_id=sample_customer.id,
            suggested_method="piva_name_mismatch",
            suggested_score=20,
            customer_name_raw="Belfiore Ristorante",
            source_platform="fatturapro",
        )
        test_db_session.add(inv)
        test_db_session.commit()

        response = test_client.get(f"/api/customers/{sample_customer.id}")
        assert response.status_code == 200
        items = response.json()["pending_suggestions"]
        assert len(items) == 1
        assert items[0]["status"] == "paid"

    def test_suggestion_for_other_customer_not_shown(
        self, test_client, test_db_session, sample_customer, quarantined_invoice
    ):
        """La quarantena di un ALTRO cliente non inquina il profilo."""
        other = Customer(ragione_sociale="Altro Cliente SRL", source="manual")
        test_db_session.add(other)
        test_db_session.commit()

        response = test_client.get(f"/api/customers/{other.id}")
        assert response.status_code == 200
        assert response.json()["pending_suggestions"] == []


class TestCreateCustomerFromInvoice:
    """POST /positions/{id}/create-customer: la terza via oltre Conferma/Rifiuta."""

    def _make_unmatched_invoice(self, session, name="YOHO MILANO S.R.L.", piva=VALID_PIVA):
        inv = Invoice(
            invoice_number="YH-001",
            amount=750.0,
            amount_due=750.0,
            status="open",
            customer_id=None,
            suggested_customer_id=999,  # suggerimento sbagliato pendente
            suggested_method="fuzzy",
            suggested_score=70,
            customer_name_raw=name,
            customer_piva_raw=piva,
            source_platform="fatturapro",
        )
        session.add(inv)
        session.commit()
        return inv

    def test_create_customer_happy_path(self, test_client, test_db_session):
        inv = self._make_unmatched_invoice(test_db_session)

        response = test_client.post(f"/api/positions/{inv.id}/create-customer")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == inv.id
        assert data["customer_name"] == "YOHO MILANO S.R.L."
        assert data["partita_iva"] == VALID_PIVA

        customer = test_db_session.query(Customer).filter(
            Customer.id == data["customer_id"]
        ).first()
        assert customer is not None
        assert customer.source == "manual"
        assert customer.ragione_sociale_normalized == "yoho milano"

        test_db_session.refresh(inv)
        assert inv.customer_id == customer.id
        assert inv.match_method == "manual"
        assert inv.match_score == 100
        assert inv.suggested_customer_id is None
        assert inv.suggested_method is None
        assert inv.suggested_score is None

        log = test_db_session.query(ActivityLog).filter(
            ActivityLog.action == "customer_created_from_invoice"
        ).first()
        assert log is not None
        assert log.entity_id == customer.id
        assert log.details["invoice_number"] == "YH-001"

    def test_invalid_piva_creates_customer_without_piva(self, test_client, test_db_session):
        """P.IVA con checksum errato: il cliente nasce SENZA P.IVA."""
        inv = self._make_unmatched_invoice(test_db_session, piva="12345678901")

        response = test_client.post(f"/api/positions/{inv.id}/create-customer")
        assert response.status_code == 200
        assert response.json()["partita_iva"] is None

    def test_400_when_already_matched(self, test_client, test_db_session, sample_invoice):
        """Fattura già abbinata: si usa Riassegna, non un nuovo cliente."""
        response = test_client.post(f"/api/positions/{sample_invoice.id}/create-customer")
        assert response.status_code == 400
        assert "già abbinata" in response.json()["detail"]

    def test_400_when_no_name(self, test_client, test_db_session):
        inv = Invoice(
            invoice_number="NN-001",
            amount=100.0,
            amount_due=100.0,
            status="open",
            customer_id=None,
            customer_name_raw=None,
            source_platform="fatturapro",
        )
        test_db_session.add(inv)
        test_db_session.commit()

        response = test_client.post(f"/api/positions/{inv.id}/create-customer")
        assert response.status_code == 400
        assert "nome destinatario" in response.json()["detail"]

    def test_404_when_invoice_missing(self, test_client):
        response = test_client.post("/api/positions/99999/create-customer")
        assert response.status_code == 404

    def test_409_duplicate_piva(self, test_client, test_db_session):
        """Stessa P.IVA validata già in anagrafica: 409 con il nome del cliente."""
        existing = Customer(
            ragione_sociale="Yoho Group SRL",
            ragione_sociale_normalized="yoho group",
            partita_iva=VALID_PIVA,
            source="shopify",
        )
        test_db_session.add(existing)
        test_db_session.commit()
        inv = self._make_unmatched_invoice(test_db_session)

        response = test_client.post(f"/api/positions/{inv.id}/create-customer")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "Yoho Group SRL" in detail
        assert "Riassegna" in detail
        # La fattura resta in quarantena, intatta
        test_db_session.refresh(inv)
        assert inv.customer_id is None
        assert inv.suggested_customer_id is not None

    def test_409_duplicate_piva_with_it_prefix(self, test_client, test_db_session):
        """Anche la variante 'IT'+P.IVA in anagrafica è la stessa entità."""
        existing = Customer(
            ragione_sociale="Yoho Import Export",
            ragione_sociale_normalized="yoho import export",
            partita_iva=f"IT{VALID_PIVA}",
            source="shopify",
        )
        test_db_session.add(existing)
        test_db_session.commit()
        inv = self._make_unmatched_invoice(test_db_session)

        response = test_client.post(f"/api/positions/{inv.id}/create-customer")
        assert response.status_code == 409
        assert "Yoho Import Export" in response.json()["detail"]

    def test_409_duplicate_normalized_name(self, test_client, test_db_session):
        """Stesso nome normalizzato: 409 con il nome del cliente esistente."""
        existing = Customer(
            ragione_sociale="YOHO MILANO",
            ragione_sociale_normalized="yoho milano",
            partita_iva=None,
            source="manual",
        )
        test_db_session.add(existing)
        test_db_session.commit()
        inv = self._make_unmatched_invoice(test_db_session, piva=None)

        response = test_client.post(f"/api/positions/{inv.id}/create-customer")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "YOHO MILANO" in detail
        assert "Riassegna" in detail

    def test_409_duplicate_name_with_stale_normalized_column(
        self, test_client, test_db_session
    ):
        """La colonna ragione_sociale_normalized in prod contiene chiavi di
        VERSIONI PRECEDENTI del normalizzatore (mai backfillate): la guardia
        deve ricalcolare la chiave al volo, non fidarsi della colonna."""
        existing = Customer(
            ragione_sociale="SHU&SHU SRL",
            # chiave scritta dal vecchio normalizzatore ('&' spaziato)
            ragione_sociale_normalized="shu & shu",
            partita_iva=None,
            source="shopify",
        )
        test_db_session.add(existing)
        test_db_session.commit()
        inv = self._make_unmatched_invoice(
            test_db_session, name="SHU & SHU S.R.L.", piva=None
        )

        response = test_client.post(f"/api/positions/{inv.id}/create-customer")
        assert response.status_code == 409
        assert "SHU&SHU SRL" in response.json()["detail"]

    def test_homonym_with_conflicting_piva_does_not_block(
        self, test_client, test_db_session
    ):
        """Omonimo con P.IVA valida DIVERSA da quella della fattura: è
        un'ALTRA entità (stessa regola di matching/auto-create) — la
        creazione del nuovo cliente non va bloccata."""
        other_piva = "98765432103"  # checksum-valida, diversa da VALID_PIVA
        existing = Customer(
            ragione_sociale="YOHO MILANO S.R.L.",
            ragione_sociale_normalized="yoho milano",
            partita_iva=other_piva,
            source="shopify",
        )
        test_db_session.add(existing)
        test_db_session.commit()
        inv = self._make_unmatched_invoice(test_db_session)  # piva=VALID_PIVA

        response = test_client.post(f"/api/positions/{inv.id}/create-customer")
        assert response.status_code == 200
        test_db_session.refresh(inv)
        assert inv.customer_id is not None
        assert inv.customer_id != existing.id


class TestLowConfidenceBadge:
    """low_confidence in /positions/suggestions: fuzzy<85 oppure score<40."""

    def _make_suggestion(self, session, customer, method, score, number):
        inv = Invoice(
            invoice_number=number,
            amount=100.0,
            amount_due=100.0,
            status="open",
            customer_id=None,
            suggested_customer_id=customer.id,
            suggested_method=method,
            suggested_score=score,
            customer_name_raw="Qualcuno SRL",
            source_platform="fatturapro",
        )
        session.add(inv)
        session.commit()
        return inv

    @pytest.mark.parametrize("method,score,expected", [
        ("fuzzy", 80, True),            # regola storica: fuzzy sotto 85
        ("fuzzy", 90, False),
        ("piva_name_mismatch", 20, True),   # NUOVO: score bassissimo, non-fuzzy
        ("piva_name_mismatch", 60, False),
        ("name_ambiguous", None, True),     # score assente = 0 → bassissimo
    ])
    def test_low_confidence_rules(
        self, test_client, test_db_session, sample_customer, method, score, expected
    ):
        inv = self._make_suggestion(test_db_session, sample_customer, method, score, "LC-1")

        response = test_client.get("/api/positions/suggestions")
        assert response.status_code == 200
        items = {i["id"]: i for i in response.json()["items"]}
        assert items[inv.id]["low_confidence"] is expected
