"""Bonifica nomi: POST /positions/{id}/assign-name-to-customer.

Via ② del menu "Risolvi" sulla riga discordante: stessa azienda, il profilo
ha il nome vecchio → si copia customer_name_raw della FATTURA sulla
ragione_sociale del CLIENTE (mai il contrario: la fattura è la prova
documentale, toccarla renderebbe la verifica circolare).

Guardrail coperti:
- 409 se le P.IVA confliggono (entrambe checksum-valide e diverse: entità
  diverse, il rinomino nasconderebbe un mis-abbinamento);
- preview→confirm: senza confirm NON applica e ritorna l'impatto (quante
  ALTRE fatture del cliente diventerebbero discordanti col nuovo nome);
- confirm applica, ricalcola la normalized, setta il lock anti-sync e logga;
- il sync clienti Shopify RISPETTA il lock (senza, il sync orario
  annullerebbe la bonifica entro un'ora).
"""

from datetime import date

from backend.config import config
from backend.database import ActivityLog, Customer, Invoice
from backend.engine.normalizer import normalize_ragione_sociale

# P.IVA italiane checksum-valide (vedi backend/engine/piva.py)
PIVA_A = "12345678903"
PIVA_B = "98765432103"

OLD_NAME = "1492 COLONIALE GROUP SRL"
NEW_NAME = "BASARA MILANO ITALIA SRL"


def _mk_customer(session, name=OLD_NAME, piva=PIVA_A, **kw):
    cust = Customer(
        ragione_sociale=name,
        ragione_sociale_normalized=normalize_ragione_sociale(name),
        partita_iva=piva,
        source=kw.pop("source", "manual"),
        **kw,
    )
    session.add(cust)
    session.commit()
    return cust


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


def _url(inv, confirm=False):
    base = f"/api/positions/{inv.id}/assign-name-to-customer"
    return f"{base}?confirm=true" if confirm else base


class TestAssignNameGuards:
    def test_404_position_not_found(self, test_client):
        r = test_client.post("/api/positions/99999/assign-name-to-customer")
        assert r.status_code == 404

    def test_400_invoice_not_linked(self, test_client, test_db_session):
        inv = _mk_invoice(test_db_session, "NL/2026", customer_name_raw=NEW_NAME)
        r = test_client.post(_url(inv))
        assert r.status_code == 400

    def test_400_invoice_without_name(self, test_client, test_db_session):
        cust = _mk_customer(test_db_session)
        inv = _mk_invoice(
            test_db_session, "NN/2026",
            customer_id=cust.id, customer_name_raw="   ",
        )
        r = test_client.post(_url(inv, confirm=True))
        assert r.status_code == 400
        test_db_session.refresh(cust)
        assert cust.ragione_sociale == OLD_NAME

    def test_409_piva_conflict_blocks_rename(self, test_client, test_db_session):
        """Entrambe le P.IVA checksum-valide e DIVERSE = entità diverse:
        rinominare nasconderebbe un mis-abbinamento. 409 anche con confirm."""
        cust = _mk_customer(test_db_session, piva=PIVA_A)
        inv = _mk_invoice(
            test_db_session, "PC/2026",
            customer_id=cust.id,
            customer_name_raw=NEW_NAME,
            customer_piva_raw=PIVA_B,
        )
        for confirm in (False, True):
            r = test_client.post(_url(inv, confirm=confirm))
            assert r.status_code == 409
        # Nulla è stato applicato: né nome, né normalized, né lock.
        test_db_session.refresh(cust)
        assert cust.ragione_sociale == OLD_NAME
        assert cust.ragione_sociale_normalized == normalize_ragione_sociale(OLD_NAME)
        assert not cust.ragione_sociale_locked

    def test_noop_when_name_already_identical(self, test_client, test_db_session):
        cust = _mk_customer(test_db_session)
        inv = _mk_invoice(
            test_db_session, "ID/2026",
            customer_id=cust.id, customer_name_raw=OLD_NAME,
        )
        r = test_client.post(_url(inv, confirm=True))
        assert r.status_code == 200
        assert r.json()["applied"] is False
        test_db_session.refresh(cust)
        # Nome già allineato: nessun lock inutile.
        assert not cust.ragione_sociale_locked


class TestAssignNamePreview:
    def _setup_coloniale(self, session):
        """Cliente col nome vecchio + la discordante BASARA + 2 fatture che
        col nuovo nome DIVENTEREBBERO discordanti + 1 pagata (non conta) +
        1 senza nome (resta warn, non conta)."""
        cust = _mk_customer(session)
        acted = _mk_invoice(
            session, "BAS/2026",
            customer_id=cust.id, customer_name_raw=NEW_NAME,
        )
        _mk_invoice(
            session, "COL1/2026",
            customer_id=cust.id, customer_name_raw=OLD_NAME,
        )
        _mk_invoice(
            session, "COL2/2026",
            customer_id=cust.id, customer_name_raw=OLD_NAME,
        )
        _mk_invoice(
            session, "COLPAID/2026",
            customer_id=cust.id, customer_name_raw=OLD_NAME, status="paid",
        )
        _mk_invoice(
            session, "NONAME/2026",
            customer_id=cust.id, customer_name_raw=None,
        )
        return cust, acted

    def test_preview_does_not_apply_and_counts_impact(
        self, test_client, test_db_session
    ):
        cust, acted = self._setup_coloniale(test_db_session)
        r = test_client.post(_url(acted))
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is False
        assert body["old_name"] == OLD_NAME
        assert body["new_name"] == NEW_NAME
        # Le due COL aperte diventerebbero discordanti; la pagata e la
        # senza-nome no.
        assert body["impact"]["would_become_discordant"] == 2
        numbers = {i["invoice_number"] for i in body["impact"]["invoices"]}
        assert numbers == {"COL1/2026", "COL2/2026"}

        # NIENTE è stato applicato.
        test_db_session.refresh(cust)
        assert cust.ragione_sociale == OLD_NAME
        assert cust.ragione_sociale_normalized == normalize_ragione_sociale(OLD_NAME)
        assert not cust.ragione_sociale_locked
        logs = test_db_session.query(ActivityLog).filter_by(
            action="audit_assign_name"
        ).all()
        assert logs == []

    def test_confirm_applies_locks_and_logs(self, test_client, test_db_session):
        cust, acted = self._setup_coloniale(test_db_session)
        r = test_client.post(_url(acted, confirm=True))
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is True
        assert body["impact"]["would_become_discordant"] == 2

        test_db_session.refresh(cust)
        assert cust.ragione_sociale == NEW_NAME
        assert cust.ragione_sociale_locked is True

        log = test_db_session.query(ActivityLog).filter_by(
            action="audit_assign_name"
        ).one()
        assert log.entity_type == "customer"
        assert log.entity_id == cust.id
        assert log.details["old_name"] == OLD_NAME
        assert log.details["new_name"] == NEW_NAME
        assert log.details["invoice_id"] == acted.id
        assert log.details["invoice_number"] == "BAS/2026"

    def test_confirm_recalculates_normalized(self, test_client, test_db_session):
        """La normalized si ricalcola col normalizzatore canonico: senza,
        il matching per nome lavorerebbe ancora sulla chiave vecchia."""
        cust = _mk_customer(test_db_session)
        acted = _mk_invoice(
            test_db_session, "BASN/2026",
            customer_id=cust.id, customer_name_raw=NEW_NAME,
        )
        r = test_client.post(_url(acted, confirm=True))
        assert r.status_code == 200
        test_db_session.refresh(cust)
        assert cust.ragione_sociale_normalized == normalize_ragione_sociale(NEW_NAME)
        assert cust.ragione_sociale_normalized == "basara milano italia"

    def test_invoice_side_never_touched(self, test_client, test_db_session):
        """customer_name_raw è la prova documentale: il confirm non lo tocca."""
        cust, acted = self._setup_coloniale(test_db_session)
        r = test_client.post(_url(acted, confirm=True))
        assert r.status_code == 200
        test_db_session.refresh(acted)
        assert acted.customer_name_raw == NEW_NAME
        assert acted.customer_id == cust.id


# ── Il sync Shopify rispetta il lock ─────────────────────────────────

SHOPIFY_ID = "gid://shopify/Customer/777"


class FakeShopifyRename:
    """Shopify riporta il nome VECCHIO: senza lock lo riscriverebbe."""

    def __init__(self, *a, **kw):
        pass

    def fetch_b2b_customers(self):
        return [{
            "shopify_id": SHOPIFY_ID,
            "ragione_sociale": OLD_NAME,
            "partita_iva": PIVA_A,
            "codice_fiscale": None,
            "codice_sdi": None,
            "phone": None,
            "phones": None,
            "email": "coloniale@example.com",
            "tags": "B2B",
        }]


class TestSyncRespectsNameLock:
    def _run_sync(self, monkeypatch, session):
        from backend.api import sync as sync_mod
        monkeypatch.setattr(sync_mod, "ShopifyConnector", FakeShopifyRename)
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: session)
        monkeypatch.setattr(config, "SHOPIFY_ACCESS_TOKEN", "test-token")
        return sync_mod._sync_customers_task()

    def test_locked_name_survives_sync(self, monkeypatch, test_db_session):
        _mk_customer(
            test_db_session, name=NEW_NAME,
            shopify_id=SHOPIFY_ID, source="shopify",
            ragione_sociale_locked=True,
        )
        result = self._run_sync(monkeypatch, test_db_session)
        assert result["success"] is True

        # re-query: il task chiude la sessione e stacca le istanze
        row = test_db_session.query(Customer).filter_by(
            shopify_id=SHOPIFY_ID
        ).one()
        # Il nome bonificato resta; il resto del ramo update gira normale
        # (l'email arriva: il lock protegge SOLO la ragione sociale).
        assert row.ragione_sociale == NEW_NAME
        assert row.ragione_sociale_normalized == normalize_ragione_sociale(NEW_NAME)
        assert row.email == "coloniale@example.com"

    def test_unlocked_name_still_updated_by_sync(self, monkeypatch, test_db_session):
        """Controprova: senza lock il sync continua ad aggiornare il nome
        (comportamento storico invariato)."""
        _mk_customer(
            test_db_session, name=NEW_NAME,
            shopify_id=SHOPIFY_ID, source="shopify",
            ragione_sociale_locked=False,
        )
        result = self._run_sync(monkeypatch, test_db_session)
        assert result["success"] is True

        row = test_db_session.query(Customer).filter_by(
            shopify_id=SHOPIFY_ID
        ).one()
        assert row.ragione_sociale == OLD_NAME
        assert row.ragione_sociale_normalized == normalize_ragione_sociale(OLD_NAME)
