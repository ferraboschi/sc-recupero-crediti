"""Tono/numerazione PER-FATTURA (Fase 2 ②).

- il numero del sollecito è quello delle fatture citate (stadio più basso+1):
  la fattura nuova riceve il SUO 1° anche se il cliente è al 2° su altre;
- stato cliente = rollup "più urgente" per-fattura (non retrocede);
- dedup giornaliero PER STADIO: 1° (nuova) e 2° (vecchie) lo stesso giorno
  sono due solleciti distinti; stesso stadio → merge;
- 'lawyer' solo se TUTTE le scadute sono consegnate; un todo legale
  pendente non basta; rete legacy senza righe di join.
"""
from datetime import date, datetime, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryAction
from backend.engine.cases import ensure_open_case, _refresh_customer_status
from backend.engine.action_invoices import set_action_invoices


@pytest.fixture
def giugno_settembre(test_db_session):
    """2 fatture di giugno al 2° sollecito + 1 nuova di settembre a 0."""
    cust = Customer(ragione_sociale="Giugno Settembre S.R.L.")
    test_db_session.add(cust)
    test_db_session.commit()
    today = date.today()
    invs = []
    for n, days in (("FT-GIU1", 90), ("FT-GIU2", 80), ("FT-SET", 3)):
        inv = Invoice(
            invoice_number=n, amount=500.0, amount_due=500.0,
            issue_date=today - timedelta(days=days + 30),
            due_date=today - timedelta(days=days), days_overdue=days,
            status="open", customer_id=cust.id, source_platform="fatturapro",
        )
        test_db_session.add(inv)
        invs.append(inv)
    test_db_session.commit()
    case = ensure_open_case(test_db_session, cust)
    old = [invs[0].id, invs[1].id]
    for k, atype in enumerate(("first_contact", "second_contact")):
        when = datetime.utcnow() - timedelta(days=30 - 10 * k)
        a = RecoveryAction(
            customer_id=cust.id, case_id=case.id, action_type=atype,
            completed_at=when, created_at=when, outcome="contacted",
            channel="whatsapp_copy", invoice_ids=old,
        )
        test_db_session.add(a)
        test_db_session.flush()
        set_action_invoices(test_db_session, a.id, old)
    cust.recovery_status = "second_contact"
    test_db_session.commit()
    return cust, invs


def _post(client, cust_id, ids):
    return client.post(f"/api/recovery/customers/{cust_id}/solleciti",
                       json={"invoice_ids": ids, "channel": "whatsapp_copy"}).json()


def test_new_invoice_gets_its_own_first(test_client, test_db_session, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    r = _post(test_client, cust.id, [s.id])
    assert r["registered"] and r["sollecito_n"] == 1
    act = test_db_session.query(RecoveryAction).get(r["action_id"])
    assert act.action_type == "first_contact"
    # rollup: le vecchie sono al 2° → il cliente NON retrocede
    test_db_session.refresh(cust)
    assert cust.recovery_status == "second_contact"


def test_old_invoice_gets_third(test_client, test_db_session, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    r = _post(test_client, cust.id, [g1.id])
    assert r["sollecito_n"] == 3
    act = test_db_session.query(RecoveryAction).get(r["action_id"])
    assert act.action_type == "second_contact"


def test_mixed_group_uses_gentlest(test_client, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    r = _post(test_client, cust.id, [g1.id, s.id])
    assert r["sollecito_n"] == 1


def test_same_day_two_stages_are_two_solleciti(test_client, test_db_session, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    r1 = _post(test_client, cust.id, [s.id])       # 1° per la nuova
    r2 = _post(test_client, cust.id, [g1.id])      # 2°(+) per la vecchia
    assert not r2.get("already_registered_today")
    assert r1["action_id"] != r2["action_id"]
    r3 = _post(test_client, cust.id, [g2.id])      # stesso stadio di r2 → merge
    assert r3.get("already_registered_today") is True
    assert r3["action_id"] == r2["action_id"]
    act = test_db_session.query(RecoveryAction).get(r2["action_id"])
    assert sorted(act.invoice_ids) == sorted([g1.id, g2.id])


def test_rollup_lawyer_only_when_all_delivered(test_client, test_db_session, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    # todo legale PENDENTE: non è "dal legale"
    test_db_session.add(RecoveryAction(customer_id=cust.id, case_id=case.id,
                                       action_type="lawyer", scheduled_date=date.today()))
    test_db_session.commit()
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "second_contact"
    # consegna parziale (le due vecchie) → resta second_contact
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                     json={"invoice_ids": [g1.id, g2.id]})
    test_db_session.refresh(cust)
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "second_contact"
    # consegna anche la nuova → lawyer
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                     json={"invoice_ids": [s.id]})
    test_db_session.refresh(cust)
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "lawyer"


def test_rollup_legacy_without_join_rows(test_db_session):
    """Pratica con 2 contatti ma NESSUNA riga di join → si usa il contatore
    di pratica (non retrocede a idle)."""
    cust = Customer(ragione_sociale="Legacy S.R.L.")
    test_db_session.add(cust)
    test_db_session.commit()
    inv = Invoice(invoice_number="FT-L", amount=100.0, amount_due=100.0,
                  issue_date=date.today() - timedelta(days=60),
                  due_date=date.today() - timedelta(days=30), days_overdue=30,
                  status="open", customer_id=cust.id, source_platform="fatturapro")
    test_db_session.add(inv)
    test_db_session.commit()
    case = ensure_open_case(test_db_session, cust)
    for atype in ("first_contact", "second_contact"):
        test_db_session.add(RecoveryAction(customer_id=cust.id, case_id=case.id,
                                           action_type=atype, completed_at=datetime.utcnow()))
    test_db_session.commit()
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "second_contact"


def test_recopy_same_invoice_same_day_does_not_escalate(test_client, test_db_session, giugno_settembre):
    """Ri-copiare lo stesso messaggio (stessa fattura) nello stesso giorno =
    stesso sollecito: la fattura NON sale di stadio in un giorno."""
    from backend.engine.action_invoices import per_invoice_sollecito_stats
    cust, (g1, g2, s) = giugno_settembre
    r1 = _post(test_client, cust.id, [s.id])
    r2 = _post(test_client, cust.id, [s.id])
    assert r2.get("already_registered_today") is True
    assert r2["action_id"] == r1["action_id"]
    assert r2["sollecito_n"] == 1
    assert per_invoice_sollecito_stats(test_db_session, [s.id])[s.id]["count"] == 1
