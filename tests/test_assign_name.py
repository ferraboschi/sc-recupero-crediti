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


def _url(inv, confirm=False, expected=None):
    """expected = expected_customer_id: il confirm lo RICHIEDE (binding
    anteprima→conferma, scenario S5 del controagente)."""
    base = f"/api/positions/{inv.id}/assign-name-to-customer"
    params = []
    if confirm:
        params.append("confirm=true")
    if expected is not None:
        params.append(f"expected_customer_id={expected}")
    return f"{base}?{'&'.join(params)}" if params else base


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
        r = test_client.post(_url(inv, confirm=True, expected=cust.id))
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
            r = test_client.post(_url(
                inv, confirm=confirm, expected=cust.id if confirm else None,
            ))
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
        r = test_client.post(_url(inv, confirm=True, expected=cust.id))
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
        r = test_client.post(_url(acted, confirm=True, expected=cust.id))
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
        r = test_client.post(_url(acted, confirm=True, expected=cust.id))
        assert r.status_code == 200
        test_db_session.refresh(cust)
        assert cust.ragione_sociale_normalized == normalize_ragione_sociale(NEW_NAME)
        assert cust.ragione_sociale_normalized == "basara milano italia"

    def test_invoice_side_never_touched(self, test_client, test_db_session):
        """customer_name_raw è la prova documentale: il confirm non lo tocca."""
        cust, acted = self._setup_coloniale(test_db_session)
        r = test_client.post(_url(acted, confirm=True, expected=cust.id))
        assert r.status_code == 200
        test_db_session.refresh(acted)
        assert acted.customer_name_raw == NEW_NAME
        assert acted.customer_id == cust.id


# ── Guard-rail omonimo (scenario S2 del controagente) ────────────────

class TestAssignNameHomonymGuard:
    """Se il VERO 'BASARA' esiste già in anagrafica, il rename creerebbe
    2 clienti con la stessa normalized → ogni futura fattura BASARA in
    quarantena name_ambiguous per sempre. Il rename era l'UNICA porta
    aperta (POST /customers e create-customer già deduplicano)."""

    def _setup(self, session, homonym_normalized="chiave-stantia"):
        cust = _mk_customer(session)  # 1492 COLONIALE, PIVA_A
        # Il vero Basara: grafia diversa E normalized STANTIA in colonna,
        # per provare che il confronto ricalcola fresh (come create-customer).
        homonym = Customer(
            ragione_sociale="Basara Milano Italia S.R.L.",
            ragione_sociale_normalized=homonym_normalized,
            partita_iva=PIVA_B,
            source="shopify",
        )
        session.add(homonym)
        session.commit()
        acted = _mk_invoice(
            session, "HM/2026",
            customer_id=cust.id, customer_name_raw=NEW_NAME,
        )
        return cust, homonym, acted

    def test_confirm_409_when_homonym_exists(self, test_client, test_db_session):
        cust, homonym, acted = self._setup(test_db_session)
        r = test_client.post(_url(acted, confirm=True, expected=cust.id))
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "Basara Milano Italia S.R.L." in detail
        assert str(homonym.id) in detail
        assert "Riassegna" in detail or "altro cliente" in detail
        # Nulla applicato: né nome né lock.
        test_db_session.refresh(cust)
        assert cust.ragione_sociale == OLD_NAME
        assert not cust.ragione_sociale_locked

    def test_preview_reports_homonym_as_info(self, test_client, test_db_session):
        cust, homonym, acted = self._setup(test_db_session)
        r = test_client.post(_url(acted))
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is False
        assert body["homonym"]["id"] == homonym.id
        assert body["homonym"]["ragione_sociale"] == "Basara Milano Italia S.R.L."

    def test_no_homonym_when_name_unique(self, test_client, test_db_session):
        cust = _mk_customer(test_db_session)
        acted = _mk_invoice(
            test_db_session, "HM2/2026",
            customer_id=cust.id, customer_name_raw=NEW_NAME,
        )
        r = test_client.post(_url(acted))
        assert r.status_code == 200
        assert r.json()["homonym"] is None


# ── Binding anteprima→conferma (scenario S5) ─────────────────────────

class TestAssignNameBinding:
    def test_confirm_400_without_expected_customer_id(
        self, test_client, test_db_session
    ):
        cust = _mk_customer(test_db_session)
        acted = _mk_invoice(
            test_db_session, "BD1/2026",
            customer_id=cust.id, customer_name_raw=NEW_NAME,
        )
        r = test_client.post(_url(acted, confirm=True))
        assert r.status_code == 400
        test_db_session.refresh(cust)
        assert cust.ragione_sociale == OLD_NAME
        assert not cust.ragione_sociale_locked

    def test_confirm_409_when_invoice_reassigned_meanwhile(
        self, test_client, test_db_session
    ):
        """Preview su X, reassign sposta la fattura su Y, confirm con
        expected=X → 409: Y (la vittima innocente) non si tocca."""
        cust_x = _mk_customer(test_db_session)
        cust_y = _mk_customer(
            test_db_session, name="ALTRO CLIENTE SRL", piva=PIVA_B,
        )
        acted = _mk_invoice(
            test_db_session, "BD2/2026",
            customer_id=cust_x.id, customer_name_raw=NEW_NAME,
        )
        # Preview mostrata all'operatore: era il cliente X.
        prev = test_client.post(_url(acted)).json()
        assert prev["customer_id"] == cust_x.id
        # Nel frattempo la fattura viene riassegnata a Y.
        acted.customer_id = cust_y.id
        test_db_session.commit()
        # Il confirm porta l'id visto in preview → deve fallire.
        r = test_client.post(_url(acted, confirm=True, expected=cust_x.id))
        assert r.status_code == 409
        assert "riassegnata" in r.json()["detail"].lower()
        test_db_session.refresh(cust_y)
        assert cust_y.ragione_sociale == "ALTRO CLIENTE SRL"
        assert not cust_y.ragione_sociale_locked


# ── Preview allineata all'audit + pagate a parte (S1/S3/S8) ──────────

class TestAssignNamePreviewHonesty:
    def test_preview_counts_paid_separately(self, test_client, test_db_session):
        """Caso owner esatto (S1): le pagate col vecchio nome non spariscono
        dal conto — restano dichiarate a parte, non mischiate alle aperte."""
        cust = _mk_customer(test_db_session)
        acted = _mk_invoice(
            test_db_session, "S1/2026",
            customer_id=cust.id, customer_name_raw=NEW_NAME,
        )
        _mk_invoice(
            test_db_session, "S1P1/2026",
            customer_id=cust.id, customer_name_raw=OLD_NAME, status="paid",
        )
        _mk_invoice(
            test_db_session, "S1P2/2026",
            customer_id=cust.id, customer_name_raw=OLD_NAME, status="paid",
        )
        body = test_client.post(_url(acted)).json()
        assert body["impact"]["would_become_discordant"] == 0
        assert body["impact"]["paid_would_become_discordant"] == 2
        paid_numbers = {
            i["invoice_number"] for i in body["impact"]["paid_invoices"]
        }
        assert paid_numbers == {"S1P1/2026", "S1P2/2026"}
        # Similarità corrente esposta per il tono d'avviso in UI.
        assert body["similarity"] == 30

    def test_preview_excludes_reviewed_like_audit(
        self, test_client, test_db_session
    ):
        """S3: una fattura già 'verificata a mano' non compare nell'audit —
        la preview non deve contarla come nuova discordante."""
        from datetime import datetime
        cust = _mk_customer(test_db_session)
        acted = _mk_invoice(
            test_db_session, "S3/2026",
            customer_id=cust.id, customer_name_raw=NEW_NAME,
        )
        _mk_invoice(
            test_db_session, "S3A/2026",
            customer_id=cust.id, customer_name_raw=OLD_NAME,
        )
        _mk_invoice(
            test_db_session, "S3B/2026",
            customer_id=cust.id, customer_name_raw=OLD_NAME,
            audit_reviewed_at=datetime.utcnow(),
        )
        body = test_client.post(_url(acted)).json()
        assert body["impact"]["would_become_discordant"] == 1
        numbers = {i["invoice_number"] for i in body["impact"]["invoices"]}
        assert numbers == {"S3A/2026"}

    def test_preview_counts_new_warnings(self, test_client, test_db_session):
        """S3: anche i peggioramenti ok→warn vanno dichiarati (contati a
        parte dalle nuove discordanti)."""
        cust = _mk_customer(test_db_session)
        acted = _mk_invoice(
            test_db_session, "S3W/2026",
            customer_id=cust.id, customer_name_raw=NEW_NAME,
        )
        # Somiglianza 82% col vecchio nome (ok), 53% col nuovo (warn).
        _mk_invoice(
            test_db_session, "S3W1/2026",
            customer_id=cust.id,
            customer_name_raw="1492 COLONIALE MILANO SRL",
        )
        body = test_client.post(_url(acted)).json()
        assert body["impact"]["would_become_discordant"] == 0
        assert body["impact"]["would_become_warning"] == 1
        warn_numbers = {
            i["invoice_number"] for i in body["impact"]["warning_invoices"]
        }
        assert warn_numbers == {"S3W1/2026"}

    def test_noop_on_case_only_difference_no_lock(
        self, test_client, test_db_session
    ):
        """S8: 'Basara Srl' vs 'BASARA SRL' = stessa normalized → no-op,
        niente rename e soprattutto niente lock inutile."""
        cust = _mk_customer(test_db_session, name="BASARA SRL", piva=PIVA_A)
        acted = _mk_invoice(
            test_db_session, "S8/2026",
            customer_id=cust.id, customer_name_raw="Basara Srl",
        )
        r = test_client.post(_url(acted, confirm=True, expected=cust.id))
        assert r.status_code == 200
        assert r.json()["applied"] is False
        test_db_session.refresh(cust)
        assert cust.ragione_sociale == "BASARA SRL"  # intatto
        assert not cust.ragione_sociale_locked


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
