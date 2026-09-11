"""Fase 5 — la riga della fattura è il registro: canali Email/Telefono, vince
l'ultimo click, nota per fattura (riga + dossier), storia per fattura."""
from datetime import date, datetime, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryAction
from backend.engine.cases import ensure_open_case


@pytest.fixture
def cli(test_db_session):
    cust = Customer(ragione_sociale="Registro S.R.L.")
    test_db_session.add(cust); test_db_session.commit()
    today = date.today()
    a = Invoice(invoice_number="FT-A", amount=100.0, amount_due=100.0, issue_date=today - timedelta(days=60),
                due_date=today - timedelta(days=30), days_overdue=30, status="open", customer_id=cust.id,
                source_platform="fatturapro")
    b = Invoice(invoice_number="FT-B", amount=50.0, amount_due=50.0, issue_date=today - timedelta(days=40),
                due_date=today - timedelta(days=10), days_overdue=10, status="open", customer_id=cust.id,
                source_platform="fatturapro")
    test_db_session.add_all([a, b]); test_db_session.commit()
    ensure_open_case(test_db_session, cust); test_db_session.commit()
    return cust, a, b


def _post(client, cid, ids, channel):
    return client.post(f"/api/recovery/customers/{cid}/solleciti", json={"invoice_ids": ids, "channel": channel})


def test_email_and_phone_channels(test_client, test_db_session, cli):
    cust, a, b = cli
    r = _post(test_client, cust.id, [a.id], "email_copy")
    assert r.status_code == 200 and r.json()["registered"] is True
    act = test_db_session.query(RecoveryAction).get(r.json()["action_id"])
    assert act.channel == "email_copy" and "via Email" in act.notes
    r2 = _post(test_client, cust.id, [b.id], "phone")
    assert r2.status_code == 200
    assert test_db_session.query(RecoveryAction).get(r2.json()["action_id"]).channel == "phone"
    assert _post(test_client, cust.id, [a.id], "fax").status_code == 400


def test_last_click_wins_same_day(test_client, test_db_session, cli):
    cust, a, b = cli
    r1 = _post(test_client, cust.id, [a.id, b.id], "whatsapp_copy").json()
    r2 = _post(test_client, cust.id, [a.id, b.id], "email_copy").json()
    assert r2["already_registered_today"] is True and r2["action_id"] == r1["action_id"]
    act = test_db_session.query(RecoveryAction).get(r1["action_id"])
    assert act.channel == "email_copy" and "via Email" in act.notes
    done = test_db_session.query(RecoveryAction).filter_by(customer_id=cust.id).filter(
        RecoveryAction.completed_at.isnot(None)).count()
    assert done == 1  # un solo sollecito, non due


def test_invoice_note_row_and_dossier(test_client, test_db_session, cli):
    from backend.api.avvocato import _customer_dossier_files
    cust, a, b = cli
    r = test_client.put(f"/api/positions/{a.id}/note", json={"note": "Paga il 20 con bonifico"})
    assert r.status_code == 200 and r.json()["recovery_note"].startswith("Paga")
    det = test_client.get(f"/api/customers/{cust.id}").json()
    assert next(i for i in det["invoices"]["items"] if i["id"] == a.id)["recovery_note"] == "Paga il 20 con bonifico"
    files = _customer_dossier_files(test_db_session, cust)
    assert any(n.startswith("dossier_") and len(d) > 500 for n, d in files)
    assert test_client.put(f"/api/positions/{a.id}/note", json={"note": "  "}).json()["recovery_note"] is None


def test_history_per_invoice(test_client, test_db_session, cli):
    cust, a, b = cli
    _post(test_client, cust.id, [a.id, b.id], "whatsapp_copy")
    # secondo sollecito "ieri": simulo spostando la data del primo
    act = test_db_session.query(RecoveryAction).filter_by(customer_id=cust.id, action_type="first_contact").first()
    act.completed_at = datetime.utcnow() - timedelta(days=3); act.created_at = act.completed_at
    test_db_session.commit()
    _post(test_client, cust.id, [a.id], "email_copy")
    det = test_client.get(f"/api/customers/{cust.id}").json()
    by = {i["id"]: i for i in det["invoices"]["items"]}
    ha = by[a.id]["history"]
    assert [h["n"] for h in ha] == [1, 2] and ha[-1]["channel"] == "email_copy"
    assert by[a.id]["last_channel"] == "email_copy" and by[a.id]["last_action_at"] is not None
    assert [h["n"] for h in by[b.id]["history"]] == [1] and by[b.id]["last_channel"] == "whatsapp_copy"
    assert by[a.id]["sollecito_count"] == 2 and by[b.id]["sollecito_count"] == 1
