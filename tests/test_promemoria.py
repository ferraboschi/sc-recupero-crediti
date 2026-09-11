# -*- coding: utf-8 -*-
"""Fatture NON scadute: promemoria pre-scadenza (todo che scatta prima del
termine, fuori dalla pratica, senza toccare lo stato del cliente)."""
from datetime import date, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryAction, RecoveryCase


@pytest.fixture
def cli(test_db_session):
    cust = Customer(ragione_sociale="In Termine S.R.L.")
    test_db_session.add(cust); test_db_session.commit()
    today = date.today()
    up = Invoice(invoice_number="FT-UP", amount=454.27, amount_due=454.27, issue_date=today - timedelta(days=2),
                 due_date=today + timedelta(days=10), due_date_source="real", days_overdue=-10, status="open",
                 customer_id=cust.id, source_platform="fatturapro")
    over = Invoice(invoice_number="FT-OVER", amount=100.0, amount_due=100.0, issue_date=today - timedelta(days=40),
                   due_date=today - timedelta(days=10), due_date_source="real", days_overdue=10, status="open",
                   customer_id=cust.id, source_platform="fatturapro")
    test_db_session.add_all([up, over]); test_db_session.commit()
    return cust, up, over


def _post(client, cid, body):
    return client.post(f"/api/recovery/customers/{cid}/actions", json=body)


def test_reminder_created_visible_and_outside_the_case(test_client, test_db_session, cli):
    cust, up, over = cli
    when = (date.today() + timedelta(days=5)).isoformat()
    r = _post(test_client, cust.id, {"action_type": "reminder", "scheduled_date": when, "invoice_ids": [up.id],
                                     "notes": "chiamare per conferma"})
    assert r.status_code == 200, r.text
    assert r.json()["action_type"] == "reminder" and r.json()["scheduled_date"] == when
    # fuori dalla pratica, stato cliente intatto
    act = test_db_session.query(RecoveryAction).get(r.json()["id"])
    assert act.case_id is None and act.completed_at is None and act.invoice_ids == [up.id]
    test_db_session.refresh(cust)
    assert cust.recovery_status == "idle"
    assert test_db_session.query(RecoveryCase).filter_by(customer_id=cust.id).count() == 0
    # visibile nella scheda (pending con fatture citate + etichetta) …
    det = test_client.get(f"/api/customers/{cust.id}").json()
    pend = [p for p in det["pending_actions"] if p["action_type"] == "reminder"]
    assert len(pend) == 1 and pend[0]["invoice_ids"] == [up.id] and pend[0]["label"] == "Promemoria pre-scadenza"
    # … e tra i todo del cruscotto (entro 14 giorni)
    todos = test_client.get("/api/dashboard/todos").json()
    flat = [t for group in todos.values() if isinstance(group, list) for t in group]
    assert any(t.get("action_type") == "reminder" and t.get("customer_id") == cust.id for t in flat)


def test_reminder_validation(test_client, cli):
    cust, up, over = cli
    today = date.today()
    # serve almeno una fattura
    assert _post(test_client, cust.id, {"action_type": "reminder"}).status_code == 400
    # solo fatture non scadute
    r = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [over.id]})
    assert r.status_code == 400 and "non ancora scadute" in r.json()["detail"].lower()
    # deve precedere la scadenza
    r = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [up.id],
                                     "scheduled_date": (today + timedelta(days=10)).isoformat()})
    assert r.status_code == 400 and "precedere" in r.json()["detail"]
    # mai nel passato
    r = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [up.id],
                                     "scheduled_date": (today - timedelta(days=1)).isoformat()})
    assert r.status_code == 400
    # default: 3 giorni prima della scadenza
    r = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [up.id]})
    assert r.status_code == 200 and r.json()["scheduled_date"] == (today + timedelta(days=7)).isoformat()
    # doppione sulla stessa fattura → 409
    r = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [up.id],
                                     "scheduled_date": (today + timedelta(days=2)).isoformat()})
    assert r.status_code == 409


def test_reminder_completed_enters_invoice_history(test_client, test_db_session, cli):
    cust, up, over = cli
    r = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [up.id]})
    aid = r.json()["id"]
    c = test_client.put(f"/api/recovery/customers/{cust.id}/actions/{aid}/complete?outcome=contacted")
    assert c.status_code == 200, c.text
    det = test_client.get(f"/api/customers/{cust.id}").json()
    inv = next(i for i in det["invoices"]["items"] if i["id"] == up.id)
    assert [h["action_type"] for h in inv["history"]] == ["reminder"]
    assert inv["history"][0]["n"] is None and inv["last_action_at"] is not None
    assert not [p for p in det["pending_actions"] if p["action_type"] == "reminder"]
    test_db_session.refresh(cust)
    assert cust.recovery_status == "idle"
