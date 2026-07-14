"""Regressioni dai finding dell'audit avversario (luglio 2026):
- undo del sollecito non deve toccare il follow-up avvocato creato dopo;
- un'azione annullata non è completabile (riattiverebbe una progressione chiusa);
- le note non sono todo: la chiusura pratica non le annulla.
"""

from datetime import datetime, date, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryAction, RecoveryCase
from backend.engine.cases import get_open_case, contact_count, ensure_open_case


@pytest.fixture
def overdue_customer(test_db_session):
    cust = Customer(ragione_sociale="Repro S.R.L.")
    test_db_session.add(cust)
    test_db_session.commit()
    today = date.today()
    invoices = []
    for i, days in enumerate([25, 12]):
        inv = Invoice(
            invoice_number=f"FT-{i+1}",
            amount=500.0, amount_due=500.0,
            issue_date=today - timedelta(days=days + 30),
            due_date=today - timedelta(days=days),
            days_overdue=days,
            status="open",
            customer_id=cust.id,
            source_platform="fatturapro",
        )
        test_db_session.add(inv)
        invoices.append(inv)
    test_db_session.commit()
    return cust, invoices


def test_undo_deletes_unrelated_lawyer_followup(test_client, test_db_session, overdue_customer):
    """F2: undo del sollecito cancella il follow-up avvocato creato DOPO da complete_action."""
    cust, invoices = overdue_customer
    case = ensure_open_case(test_db_session, cust)
    # pratica con todo legale pendente (passaggio all'avvocato pianificato)
    lawyer_todo = RecoveryAction(
        customer_id=cust.id, case_id=case.id, action_type="lawyer",
        scheduled_date=date.today(),
    )
    test_db_session.add(lawyer_todo)
    test_db_session.commit()

    # 09:00 — sollecito WhatsApp registrato (il todo legale resta)
    reg = test_client.post(
        f"/api/recovery/customers/{cust.id}/solleciti",
        json={"invoice_ids": [invoices[0].id], "channel": "whatsapp_copy"},
    ).json()
    assert reg["registered"] is True

    # 11:00 — l'operatore chiama l'avvocato e completa il todo legale:
    # si crea il follow-up "Auto-pianificata: follow-up avvocato"
    comp = test_client.put(
        f"/api/recovery/customers/{cust.id}/actions/{lawyer_todo.id}/complete",
        params={"outcome": "contacted"},
    ).json()
    followup_id = comp["next_action"]["id"]

    # 15:00 — undo del sollecito del mattino
    undo = test_client.delete(
        f"/api/recovery/customers/{cust.id}/solleciti/{reg['action_id']}"
    ).json()

    followup = test_db_session.query(RecoveryAction).filter_by(id=followup_id).first()
    print("removed_next_actions:", undo["removed_next_actions"])
    assert followup is not None, (
        "BUG: l'undo del sollecito ha CANCELLATO il follow-up avvocato "
        "creato dal completamento del todo legale"
    )


def test_complete_cancelled_action_resurrects_archived_customer(test_client, test_db_session, overdue_customer):
    """F1: completare un'azione ANNULLATA (bottone visibile in UI) riapre
    una pratica per un cliente archiviato e pianifica un'azione."""
    cust, invoices = overdue_customer
    case = ensure_open_case(test_db_session, cust)
    pending = RecoveryAction(
        customer_id=cust.id, case_id=case.id, action_type="second_contact",
        scheduled_date=date.today() + timedelta(days=3),
    )
    test_db_session.add(pending)
    test_db_session.commit()

    # L'operatore archivia il cliente (inesigibile → chiude la pratica,
    # annulla i todo)
    resp = test_client.post(
        f"/api/recovery/customers/{cust.id}/actions",
        json={"action_type": "archive", "scheduled_date": None, "notes": "inesigibile"},
    )
    assert resp.status_code == 200
    test_db_session.expire_all()
    assert cust.recovery_status == "archived"
    test_db_session.refresh(pending)
    assert pending.cancelled is True  # annullata dalla chiusura

    # In UI l'azione annullata mostra comunque "Completa" → click
    comp = test_client.put(
        f"/api/recovery/customers/{cust.id}/actions/{pending.id}/complete",
        params={"outcome": "contacted"},
    )
    print("complete su azione annullata →", comp.status_code, comp.json())

    test_db_session.expire_all()
    new_open = get_open_case(test_db_session, cust.id)
    test_db_session.refresh(cust)
    print("recovery_status:", cust.recovery_status,
          "| nuova pratica aperta:", new_open.id if new_open else None,
          "| next:", cust.next_action_type, cust.next_action_date)
    assert comp.status_code != 200 or new_open is None, (
        "BUG: cliente archiviato resuscitato completando un'azione annullata"
    )


def test_close_case_cancels_customer_notes(test_client, test_db_session, overdue_customer):
    """F3: la chiusura a saldo annulla anche le NOTE (caseless, mai completate)."""
    from backend.engine.cases import update_case_lifecycle
    cust, invoices = overdue_customer
    test_client.post(
        f"/api/recovery/customers/{cust.id}/solleciti",
        json={"invoice_ids": [invoices[0].id], "channel": "whatsapp_copy"},
    )
    note_resp = test_client.post(
        f"/api/recovery/customers/{cust.id}/actions",
        json={"action_type": "note", "scheduled_date": None, "notes": "promette bonifico venerdì"},
    ).json()

    for inv in invoices:
        inv.status = "paid"
        inv.amount_due = 0
        inv.days_overdue = 0
    test_db_session.commit()
    update_case_lifecycle(test_db_session)

    note = test_db_session.query(RecoveryAction).filter_by(id=note_resp["id"]).first()
    print("nota cancelled:", note.cancelled, "| notes:", note.notes)
    assert not note.cancelled, "BUG: nota dell'operatore annullata dalla chiusura pratica"
