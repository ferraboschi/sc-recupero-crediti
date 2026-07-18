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
