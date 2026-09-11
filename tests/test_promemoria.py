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
    # nel registro sì, ma NON è l'"ultima azione" di recupero della riga
    assert inv["history"][0]["n"] is None and inv["last_action_at"] is None
    assert not [p for p in det["pending_actions"] if p["action_type"] == "reminder"]
    test_db_session.refresh(cust)
    assert cust.recovery_status == "idle"


# ── Trovati dalla review avversariale ───────────────────────────────────────

def test_reminder_survives_case_close(test_client, test_db_session, cli):
    """Il promemoria vive fuori dalla pratica: la chiusura a saldo (sync o
    archivio) non lo annulla."""
    from backend.engine.cases import ensure_open_case, update_case_lifecycle
    cust, up, over = cli
    ensure_open_case(test_db_session, cust); test_db_session.commit()
    r = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [up.id]})
    aid = r.json()["id"]
    over.status = "paid"; over.amount_due = 0; over.days_overdue = 0
    test_db_session.commit()
    stats = update_case_lifecycle(test_db_session)
    assert stats["closed"] == 1
    act = test_db_session.query(RecoveryAction).get(aid)
    assert act.cancelled is not True and act.completed_at is None
    det = test_client.get(f"/api/customers/{cust.id}").json()
    assert [p["action_type"] for p in det["pending_actions"]] == ["reminder"]


def test_reminder_auto_settles_when_invoice_paid(test_client, test_db_session, cli):
    """Fattura saldata prima della scadenza → il promemoria si chiude da solo
    (completato, esito 'paid', nota), non resta uno zombie tra i todo."""
    from backend.engine.cases import update_case_lifecycle
    cust, up, over = cli
    aid = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [up.id]}).json()["id"]
    up.status = "paid"; up.amount_due = 0
    test_db_session.commit()
    stats = update_case_lifecycle(test_db_session)
    assert stats["reminders_settled"] == 1
    act = test_db_session.query(RecoveryAction).get(aid)
    assert act.completed_at is not None and act.outcome == "paid" and "saldata" in act.notes
    todos = test_client.get("/api/dashboard/todos").json()
    flat = [t for group in todos.values() if isinstance(group, list) for t in group]
    assert not [t for t in flat if t.get("action_type") == "reminder"]


def test_reminder_reschedule_guarded_and_isolated(test_client, test_db_session, cli):
    """Spostare il promemoria non tocca next_action_date del cliente e resta
    nella finestra [oggi, scadenza)."""
    cust, up, over = cli
    today = date.today()
    cust.next_action_date = today + timedelta(days=30); cust.next_action_type = "second_contact"
    test_db_session.commit()
    aid = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [up.id]}).json()["id"]
    url = f"/api/recovery/customers/{cust.id}/actions/{aid}/reschedule"
    r = test_client.patch(url, params={"new_date": (today + timedelta(days=2)).isoformat()})
    assert r.status_code == 200, r.text
    test_db_session.refresh(cust)
    assert cust.next_action_date == today + timedelta(days=30)
    assert test_client.patch(url, params={"new_date": (today + timedelta(days=10)).isoformat()}).status_code == 400
    assert test_client.patch(url, params={"new_date": (today - timedelta(days=1)).isoformat()}).status_code == 400


def test_reminder_excluded_customer_and_todo_payload(test_client, test_db_session, cli):
    cust, up, over = cli
    r = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [up.id],
                                     "scheduled_date": (date.today() + timedelta(days=3)).isoformat()})
    assert r.status_code == 200
    todos = test_client.get("/api/dashboard/todos").json()
    flat = [t for group in todos.values() if isinstance(group, list) for t in group]
    t = next(t for t in flat if t.get("action_type") == "reminder")
    assert t["invoices"][0]["invoice_number"] == "FT-UP" and t["invoices"][0]["amount_due"] == 454.27
    cust.excluded = True; test_db_session.commit()
    r = _post(test_client, cust.id, {"action_type": "reminder", "invoice_ids": [up.id]})
    assert r.status_code == 409
