"""BONIFICA DUREVOLE dell'anagrafica.

La tesi: "confermato" deve diventare un tratto d'IDENTITÀ del CLIENTE, non
uno stato per-fattura (audit_reviewed_at) che le fatture future ignorano.
Il cliente ha due tratti che le fatture usano per abbinarsi/verificarsi — la
P.IVA e le intestazioni accettate — e qui si bonificano entrambi:

- TIER 1 (P.IVA): bonifica_piva a livello cliente nell'audit + cascade
  dell'endpoint esistente assign-piva-to-customer (verify dal vivo → verde
  vero, checksum, anche sulle future).
- TIER 2 (intestazioni accettate): nuovo tratto CustomerAcceptedName letto
  DAL VIVO dal verificatore → conferma umana durevole (present + future),
  con la valvola P.IVA-conflitto e la reversibilità.
"""

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from backend.database import Customer, CustomerAcceptedName, Invoice
from backend.engine.normalizer import normalize_ragione_sociale

# P.IVA italiane checksum-valide (vedi backend/engine/piva.py)
PIVA_A = "12345678903"
PIVA_B = "98765432103"
# La P.IVA del caso reale dell'owner (Cavo Luigi Beverage Solutions srl)
PIVA_CAVO = "02572440994"


def _customer(session, name, **kw):
    c = Customer(ragione_sociale=name, source=kw.pop("source", "shopify"), **kw)
    session.add(c)
    session.commit()
    return c


def _invoice(session, number, customer_id=None, **kw):
    inv = Invoice(
        invoice_number=number,
        amount=kw.pop("amount", 100.0),
        amount_due=kw.pop("amount_due", 100.0),
        issue_date=kw.pop("issue_date", date(2026, 4, 1)),
        due_date=kw.pop("due_date", date(2026, 5, 1)),
        days_overdue=kw.pop("days_overdue", 10),
        source_platform=kw.pop("source_platform", "fatturapro"),
        status=kw.pop("status", "open"),
        customer_id=customer_id,
        **kw,
    )
    session.add(inv)
    session.commit()
    return inv


# ── Modello CustomerAcceptedName ─────────────────────────────────────

class TestAcceptedNameModel:
    def test_relationship_and_persist(self, test_db_session):
        c = _customer(test_db_session, "Cavo Luigi Beverage Solutions srl")
        an = CustomerAcceptedName(
            customer_id=c.id,
            name_normalized=normalize_ragione_sociale("Cavo Luigi"),
            note="Cavo Luigi",
        )
        test_db_session.add(an)
        test_db_session.commit()
        test_db_session.refresh(c)
        # La relationship Customer.accepted_names è navigabile.
        assert [a.name_normalized for a in c.accepted_names] == [
            normalize_ragione_sociale("Cavo Luigi")
        ]
        assert c.accepted_names[0].note == "Cavo Luigi"

    def test_unique_customer_name(self, test_db_session):
        # (customer_id, name_normalized) UNIQUE: niente doppioni della stessa
        # intestazione — l'add è idempotente per costruzione.
        c = _customer(test_db_session, "Rossi SRL")
        key = normalize_ragione_sociale("Mario Rossi")
        test_db_session.add(CustomerAcceptedName(customer_id=c.id, name_normalized=key))
        test_db_session.commit()
        test_db_session.add(CustomerAcceptedName(customer_id=c.id, name_normalized=key))
        with pytest.raises(IntegrityError):
            test_db_session.commit()
        test_db_session.rollback()

    def test_same_name_different_customers_allowed(self, test_db_session):
        # L'UNIQUE è per-cliente: due clienti diversi possono accettare la
        # stessa grafia (omonimi legittimi).
        c1 = _customer(test_db_session, "Uno SRL")
        c2 = _customer(test_db_session, "Due SRL")
        key = normalize_ragione_sociale("Insegna Comune")
        test_db_session.add(CustomerAcceptedName(customer_id=c1.id, name_normalized=key))
        test_db_session.add(CustomerAcceptedName(customer_id=c2.id, name_normalized=key))
        test_db_session.commit()  # nessun IntegrityError
        assert test_db_session.query(CustomerAcceptedName).count() == 2

    def test_cascade_delete_with_customer(self, test_db_session):
        # delete-orphan: cancellato il cliente, le sue intestazioni non restano
        # orfane.
        c = _customer(test_db_session, "Effimero SRL")
        c.accepted_names.append(
            CustomerAcceptedName(name_normalized=normalize_ragione_sociale("Effimero"))
        )
        test_db_session.commit()
        assert test_db_session.query(CustomerAcceptedName).count() == 1
        test_db_session.delete(c)
        test_db_session.commit()
        assert test_db_session.query(CustomerAcceptedName).count() == 0


# ── Endpoint POST/DELETE accepted-names + lista su GET customer ───────

from backend.database import ActivityLog  # noqa: E402

# Intestazione grezza e cliente con nome dissimile senza P.IVA: senza la
# conferma d'identità la fattura è discordante (bad), con la conferma esce.
HEADING = "Sushi Kyoto"
CUST_NAME = "Ferramenta Bianchi"


class TestAcceptedNameEndpoints:
    def test_add_by_name_persists_and_logs(self, test_client, test_db_session):
        c = _customer(test_db_session, CUST_NAME)
        r = test_client.post(
            f"/api/customers/{c.id}/accepted-names", json={"name": HEADING}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["already_present"] is False
        assert body["accepted_name"]["name_normalized"] == normalize_ragione_sociale(HEADING)
        assert body["accepted_name"]["note"] == HEADING
        # La lista aggiornata torna nella risposta.
        assert len(body["accepted_names"]) == 1
        # Persistito + loggato.
        assert test_db_session.query(CustomerAcceptedName).filter_by(customer_id=c.id).count() == 1
        log = test_db_session.query(ActivityLog).filter_by(action="audit_accept_name").one()
        assert log.entity_type == "customer"
        assert log.entity_id == c.id

    def test_add_by_invoice_id_takes_raw_name(self, test_client, test_db_session):
        c = _customer(test_db_session, CUST_NAME)
        inv = _invoice(
            test_db_session, "SK/2026", customer_id=c.id, customer_name_raw=HEADING,
        )
        r = test_client.post(
            f"/api/customers/{c.id}/accepted-names", json={"invoice_id": inv.id}
        )
        assert r.status_code == 200
        assert r.json()["accepted_name"]["name_normalized"] == normalize_ragione_sociale(HEADING)
        assert r.json()["accepted_name"]["note"] == HEADING

    def test_add_is_idempotent(self, test_client, test_db_session):
        c = _customer(test_db_session, CUST_NAME)
        test_client.post(f"/api/customers/{c.id}/accepted-names", json={"name": HEADING})
        # Seconda add stessa intestazione (anche con grafia diversa ma stessa
        # normalized): no-op, nessun doppione.
        r = test_client.post(
            f"/api/customers/{c.id}/accepted-names", json={"name": "sushi   kyoto"}
        )
        assert r.status_code == 200
        assert r.json()["already_present"] is True
        assert test_db_session.query(CustomerAcceptedName).filter_by(customer_id=c.id).count() == 1

    def test_add_404_missing_customer(self, test_client):
        assert test_client.post(
            "/api/customers/9999/accepted-names", json={"name": HEADING}
        ).status_code == 404

    def test_add_400_empty_name(self, test_client, test_db_session):
        c = _customer(test_db_session, CUST_NAME)
        assert test_client.post(
            f"/api/customers/{c.id}/accepted-names", json={"name": "   "}
        ).status_code == 400

    def test_add_400_without_name_or_invoice(self, test_client, test_db_session):
        c = _customer(test_db_session, CUST_NAME)
        assert test_client.post(
            f"/api/customers/{c.id}/accepted-names", json={}
        ).status_code == 400

    def test_add_400_invoice_without_name(self, test_client, test_db_session):
        c = _customer(test_db_session, CUST_NAME)
        inv = _invoice(
            test_db_session, "NN/2026", customer_id=c.id, customer_name_raw="   ",
        )
        assert test_client.post(
            f"/api/customers/{c.id}/accepted-names", json={"invoice_id": inv.id}
        ).status_code == 400

    def test_remove_by_id_reversible_and_logs(self, test_client, test_db_session):
        c = _customer(test_db_session, CUST_NAME)
        add = test_client.post(
            f"/api/customers/{c.id}/accepted-names", json={"name": HEADING}
        ).json()
        an_id = add["accepted_name"]["id"]
        r = test_client.delete(f"/api/customers/{c.id}/accepted-names/{an_id}")
        assert r.status_code == 200
        assert r.json()["accepted_names"] == []
        assert test_db_session.query(CustomerAcceptedName).filter_by(customer_id=c.id).count() == 0
        assert test_db_session.query(ActivityLog).filter_by(action="audit_unaccept_name").count() == 1

    def test_remove_by_normalized_name(self, test_client, test_db_session):
        c = _customer(test_db_session, CUST_NAME)
        test_client.post(f"/api/customers/{c.id}/accepted-names", json={"name": HEADING})
        key = normalize_ragione_sociale(HEADING)
        r = test_client.delete(f"/api/customers/{c.id}/accepted-names/{key}")
        assert r.status_code == 200
        assert test_db_session.query(CustomerAcceptedName).filter_by(customer_id=c.id).count() == 0

    def test_remove_404_when_absent(self, test_client, test_db_session):
        c = _customer(test_db_session, CUST_NAME)
        assert test_client.delete(
            f"/api/customers/{c.id}/accepted-names/99999"
        ).status_code == 404

    def test_get_customer_exposes_accepted_names(self, test_client, test_db_session):
        c = _customer(test_db_session, CUST_NAME)
        test_client.post(f"/api/customers/{c.id}/accepted-names", json={"name": HEADING})
        detail = test_client.get(f"/api/customers/{c.id}").json()
        assert "accepted_names" in detail
        assert len(detail["accepted_names"]) == 1
        assert detail["accepted_names"][0]["note"] == HEADING


class TestAcceptedNameBulkCascade:
    def test_one_accept_greens_present_and_future(self, test_client, test_db_session):
        # Punto 4: aggiungere UN'intestazione rende verdi TUTTE le fatture
        # aperte con quella intestazione in un colpo, presenti E future —
        # nessuna scrittura per-fattura (verify legge la lista dal vivo).
        c = _customer(test_db_session, CUST_NAME)  # nessuna P.IVA
        i1 = _invoice(test_db_session, "P1/2026", customer_id=c.id, customer_name_raw=HEADING)
        i2 = _invoice(test_db_session, "P2/2026", customer_id=c.id, customer_name_raw=HEADING)
        # Prima dell'accept: entrambe discordanti.
        detail = test_client.get(f"/api/customers/{c.id}").json()
        for it in detail["invoices"]["items"]:
            assert it["verification"]["level"] == "critical"
        # UN SOLO accept (da una qualsiasi delle due).
        test_client.post(f"/api/customers/{c.id}/accepted-names", json={"invoice_id": i1.id})
        detail = test_client.get(f"/api/customers/{c.id}").json()
        by_num = {it["invoice_number"]: it for it in detail["invoices"]["items"]}
        for num in ("P1/2026", "P2/2026"):
            assert by_num[num]["verification"]["level"] == "verified"
            assert by_num[num]["verification"]["manual_confirmed"] is True
        # Fattura FUTURA con la stessa intestazione: verde anche lei, senza
        # nessun'altra azione (la lista è consultata dal vivo).
        _invoice(test_db_session, "FUT/2026", customer_id=c.id, customer_name_raw=HEADING)
        detail = test_client.get(f"/api/customers/{c.id}").json()
        by_num = {it["invoice_number"]: it for it in detail["invoices"]["items"]}
        assert by_num["FUT/2026"]["verification"]["level"] == "verified"


class TestAcceptedNameExitsAudit:
    def test_accepted_customer_leaves_da_sanificare(self, test_client, test_db_session):
        # Punto 5: un cliente le cui uniche fatture problematiche sono su
        # intestazioni accettate ESCE da "da sanificare" — l'audit eredita la
        # whitelist perché passa da verify.
        c = _customer(test_db_session, CUST_NAME)
        _invoice(test_db_session, "AUD/2026", customer_id=c.id, customer_name_raw=HEADING)
        # Prima: dentro tutti e tre gli insiemi dell'audit.
        assert c.id in test_client.get("/api/customers/audit-summary").json()["customer_ids"]
        assert test_client.get(f"/api/customers/{c.id}/audit").json()["worst_verdict"] != "ok"
        ids = [i["id"] for i in test_client.get(
            "/api/customers?to_sanitize=true&only_overdue=false"
        ).json()["items"]]
        assert c.id in ids
        # Accetto l'intestazione.
        test_client.post(f"/api/customers/{c.id}/accepted-names", json={"name": HEADING})
        # Dopo: fuori da tutti e tre.
        assert c.id not in test_client.get("/api/customers/audit-summary").json()["customer_ids"]
        aud = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert aud["problem_count"] == 0
        assert aud["worst_verdict"] == "ok"
        ids = [i["id"] for i in test_client.get(
            "/api/customers?to_sanitize=true&only_overdue=false"
        ).json()["items"]]
        assert c.id not in ids
