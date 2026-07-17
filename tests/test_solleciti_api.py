"""Test API registrazione solleciti (Copia Messaggio) e azioni con pratiche."""

from datetime import datetime, date, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryAction
from backend.engine.cases import get_open_case, contact_count


@pytest.fixture
def overdue_customer(test_db_session):
    """Cliente con due fatture scadute."""
    cust = Customer(ragione_sociale="Rooftop S.R.L.")
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


class TestRegisterSollecito:
    def test_first_sollecito_registered_and_next_scheduled(self, test_client, test_db_session, overdue_customer):
        cust, invoices = overdue_customer

        resp = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [inv.id for inv in invoices], "channel": "whatsapp_copy"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["registered"] is True
        assert data["sollecito_n"] == 1
        assert data["next_action"]["action_type"] == "second_contact"

        case = get_open_case(test_db_session, cust.id)
        assert case is not None
        action = test_db_session.query(RecoveryAction).filter_by(id=data["action_id"]).first()
        assert action.completed_at is not None
        assert action.action_type == "first_contact"
        assert action.channel == "whatsapp_copy"
        assert sorted(action.invoice_ids) == sorted(inv.id for inv in invoices)
        test_db_session.refresh(cust)
        assert cust.recovery_status == "first_contact"

    def test_same_day_dedup_returns_same_number(self, test_client, test_db_session, overdue_customer):
        cust, invoices = overdue_customer
        first = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [invoices[0].id], "channel": "whatsapp_copy"},
        ).json()

        second = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [invoices[1].id], "channel": "whatsapp_link"},
        ).json()

        assert second["registered"] is True
        assert second.get("already_registered_today") is True
        assert second["sollecito_n"] == first["sollecito_n"] == 1
        # le fatture citate si sommano sull'azione esistente
        action = test_db_session.query(RecoveryAction).filter_by(id=first["action_id"]).first()
        test_db_session.refresh(action)
        assert sorted(action.invoice_ids) == sorted([invoices[0].id, invoices[1].id])
        # UNA sola azione contatto registrata
        n_actions = test_db_session.query(RecoveryAction).filter(
            RecoveryAction.customer_id == cust.id,
            RecoveryAction.completed_at.isnot(None),
        ).count()
        assert n_actions == 1

    def test_supersedes_pending_contact_but_not_lawyer(self, test_client, test_db_session, overdue_customer):
        cust, invoices = overdue_customer
        # pratica con todo contatto pendente E todo legale
        from backend.engine.cases import ensure_open_case
        case = ensure_open_case(test_db_session, cust)
        pending_contact = RecoveryAction(
            customer_id=cust.id, case_id=case.id, action_type="second_contact",
            scheduled_date=date.today() + timedelta(days=2),
        )
        pending_lawyer = RecoveryAction(
            customer_id=cust.id, case_id=case.id, action_type="lawyer",
            scheduled_date=date.today() + timedelta(days=9),
        )
        test_db_session.add_all([pending_contact, pending_lawyer])
        test_db_session.commit()

        resp = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [invoices[0].id], "channel": "whatsapp_copy"},
        )

        data = resp.json()
        assert data["registered"] is True
        test_db_session.refresh(pending_contact)
        test_db_session.refresh(pending_lawyer)
        # il todo contatto è soppiantato...
        assert pending_contact.cancelled is True
        assert pending_contact.cancelled_reason.startswith("superseded_by_sollecito")
        # ...il todo legale resta intatto e resta il next step
        assert pending_lawyer.cancelled is False
        assert data["next_action"]["action_type"] == "lawyer"

    def test_no_overdue_returns_not_registered(self, test_client, test_db_session):
        cust = Customer(ragione_sociale="Cliente Puntuale SRL")
        test_db_session.add(cust)
        test_db_session.commit()

        resp = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [], "channel": "whatsapp_copy"},
        )

        assert resp.status_code == 200
        assert resp.json()["registered"] is False
        assert get_open_case(test_db_session, cust.id) is None

    def test_excluded_customer_409(self, test_client, test_db_session, overdue_customer):
        cust, _ = overdue_customer
        cust.excluded = True
        test_db_session.commit()

        resp = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [], "channel": "whatsapp_copy"},
        )
        assert resp.status_code == 409

    def test_invalid_channel_400(self, test_client, overdue_customer):
        cust, _ = overdue_customer
        resp = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [], "channel": "telegram"},
        )
        assert resp.status_code == 400

    def test_sollecito_rejects_invoices_of_another_customer(
        self, test_client, test_db_session, overdue_customer
    ):
        """Il sollecito deve citare solo fatture DEL cliente.

        Regressione: la storia della pratica si inquinava con fatture altrui, e
        il frontend in race (ClientDetail.jsx fetch senza cancellazione) può
        davvero mandarle: mostra il cliente 2 mentre l'URL dice 3.
        """
        cust_a, invoices_a = overdue_customer

        # Cliente B, con una fattura scaduta tutta sua
        cust_b = Customer(ragione_sociale="Altro Cliente SRL")
        test_db_session.add(cust_b)
        test_db_session.commit()
        inv_b = Invoice(
            invoice_number="FT-B1",
            amount=900.0, amount_due=900.0,
            issue_date=date.today() - timedelta(days=60),
            due_date=date.today() - timedelta(days=30),
            days_overdue=30,
            status="open",
            customer_id=cust_b.id,
            source_platform="fatturapro",
        )
        test_db_session.add(inv_b)
        test_db_session.commit()

        resp = test_client.post(
            f"/api/recovery/customers/{cust_a.id}/solleciti",
            json={"invoice_ids": [inv_b.id, 999999], "channel": "whatsapp_copy"},
        )

        assert resp.status_code == 400
        assert "999999" in resp.json()["detail"]
        # nessuna azione registrata: la pratica di A resta pulita
        assert test_db_session.query(RecoveryAction).filter(
            RecoveryAction.customer_id == cust_a.id
        ).count() == 0
        assert get_open_case(test_db_session, cust_a.id) is None

    def test_sollecito_accepts_own_invoices(
        self, test_client, test_db_session, overdue_customer
    ):
        """Il caso positivo: le fatture PROPRIE passano e vengono registrate."""
        cust, invoices = overdue_customer

        resp = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [inv.id for inv in invoices], "channel": "whatsapp_copy"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["registered"] is True
        action = test_db_session.query(RecoveryAction).filter_by(id=data["action_id"]).first()
        assert sorted(action.invoice_ids) == sorted(inv.id for inv in invoices)

    def test_sollecito_dedup_rejects_foreign_invoices(
        self, test_client, test_db_session, overdue_customer
    ):
        """Anche il merge del dedup giornaliero va protetto: il secondo copy
        dello stesso giorno NON deve poter iniettare fatture altrui
        nell'azione già registrata."""
        cust_a, invoices_a = overdue_customer

        cust_b = Customer(ragione_sociale="Terzo Cliente SRL")
        test_db_session.add(cust_b)
        test_db_session.commit()
        inv_b = Invoice(
            invoice_number="FT-C1",
            amount=100.0, amount_due=100.0,
            issue_date=date.today() - timedelta(days=50),
            due_date=date.today() - timedelta(days=20),
            days_overdue=20,
            status="open",
            customer_id=cust_b.id,
            source_platform="fatturapro",
        )
        test_db_session.add(inv_b)
        test_db_session.commit()

        first = test_client.post(
            f"/api/recovery/customers/{cust_a.id}/solleciti",
            json={"invoice_ids": [invoices_a[0].id], "channel": "whatsapp_copy"},
        ).json()

        second = test_client.post(
            f"/api/recovery/customers/{cust_a.id}/solleciti",
            json={"invoice_ids": [inv_b.id], "channel": "whatsapp_link"},
        )

        assert second.status_code == 400
        action = test_db_session.query(RecoveryAction).filter_by(id=first["action_id"]).first()
        test_db_session.refresh(action)
        assert action.invoice_ids == [invoices_a[0].id]


class TestUndoSollecito:
    def test_undo_restores_state(self, test_client, test_db_session, overdue_customer):
        cust, invoices = overdue_customer
        from backend.engine.cases import ensure_open_case
        case = ensure_open_case(test_db_session, cust)
        pending = RecoveryAction(
            customer_id=cust.id, case_id=case.id, action_type="first_contact",
            scheduled_date=date.today() + timedelta(days=1),
        )
        test_db_session.add(pending)
        test_db_session.commit()

        reg = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [invoices[0].id], "channel": "whatsapp_copy"},
        ).json()
        assert contact_count(test_db_session, case) == 1

        undo = test_client.delete(
            f"/api/recovery/customers/{cust.id}/solleciti/{reg['action_id']}"
        )

        assert undo.status_code == 200
        assert undo.json()["undone"] is True
        test_db_session.expire_all()
        # contatore tornato a zero, todo soppiantato ripristinato,
        # next auto-pianificata rimossa
        assert contact_count(test_db_session, case) == 0
        test_db_session.refresh(pending)
        assert pending.cancelled is False
        auto_next = test_db_session.query(RecoveryAction).filter(
            RecoveryAction.case_id == case.id,
            RecoveryAction.notes.like("Auto-pianificata%"),
        ).count()
        assert auto_next == 0

    def test_undo_only_same_day_whatsapp(self, test_client, test_db_session, overdue_customer):
        cust, invoices = overdue_customer
        reg = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [invoices[0].id], "channel": "whatsapp_copy"},
        ).json()
        action = test_db_session.query(RecoveryAction).filter_by(id=reg["action_id"]).first()
        action.completed_at = datetime.utcnow() - timedelta(days=2)
        test_db_session.commit()

        undo = test_client.delete(
            f"/api/recovery/customers/{cust.id}/solleciti/{reg['action_id']}"
        )
        assert undo.status_code == 400


class TestCycleReset:
    def test_full_cycle_numbering_resets_after_payment(self, test_client, test_db_session, overdue_customer):
        """Il percorso completo del punto 3: sollecito → saldo → pratica
        chiusa → nuova fattura → il sollecito riparte da n. 1."""
        from backend.engine.cases import update_case_lifecycle

        cust, invoices = overdue_customer
        first = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [inv.id for inv in invoices], "channel": "whatsapp_copy"},
        ).json()
        assert first["sollecito_n"] == 1
        old_case = get_open_case(test_db_session, cust.id)

        # saldo completo
        for inv in invoices:
            inv.status = "paid"
            inv.amount_due = 0
            inv.days_overdue = 0
        test_db_session.commit()
        update_case_lifecycle(test_db_session)
        test_db_session.refresh(old_case)
        assert old_case.status == "closed"
        assert old_case.closed_reason == "paid"
        # simuliamo il passare del tempo oltre la finestra anti-flapping
        old_case.closed_at = datetime.utcnow() - timedelta(days=45)
        test_db_session.commit()

        # nuova fattura scaduta, mesi dopo
        new_inv = Invoice(
            invoice_number="FT-NUOVA",
            amount=800.0, amount_due=800.0,
            issue_date=date.today() - timedelta(days=40),
            due_date=date.today() - timedelta(days=10),
            days_overdue=10, status="open",
            customer_id=cust.id, source_platform="fatturapro",
        )
        test_db_session.add(new_inv)
        test_db_session.commit()
        update_case_lifecycle(test_db_session)

        second = test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [new_inv.id], "channel": "whatsapp_copy"},
        ).json()

        # NON è il "secondo sollecito": è la PRIMA azione della pratica nuova
        assert second["sollecito_n"] == 1
        new_case = get_open_case(test_db_session, cust.id)
        assert new_case.id != old_case.id


class TestCreateActionGuards:
    def test_contact_action_409_when_pending_contact_exists(self, test_client, test_db_session, overdue_customer):
        cust, _ = overdue_customer
        first = test_client.post(
            f"/api/recovery/customers/{cust.id}/actions",
            json={"action_type": "first_contact", "scheduled_date": None, "notes": None},
        )
        assert first.status_code == 200

        dup = test_client.post(
            f"/api/recovery/customers/{cust.id}/actions",
            json={"action_type": "second_contact", "scheduled_date": None, "notes": None},
        )
        assert dup.status_code == 409

    def test_contact_action_409_after_sollecito_today(self, test_client, test_db_session, overdue_customer):
        cust, invoices = overdue_customer
        test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [invoices[0].id], "channel": "whatsapp_copy"},
        )

        resp = test_client.post(
            f"/api/recovery/customers/{cust.id}/actions",
            json={"action_type": "second_contact", "scheduled_date": None, "notes": None},
        )
        # il sollecito di oggi ha già pianificato il prossimo passo
        assert resp.status_code == 409

    def test_archive_closes_case(self, test_client, test_db_session, overdue_customer):
        cust, _ = overdue_customer
        from backend.engine.cases import ensure_open_case
        case = ensure_open_case(test_db_session, cust)
        test_db_session.commit()

        resp = test_client.post(
            f"/api/recovery/customers/{cust.id}/actions",
            json={"action_type": "archive", "scheduled_date": None, "notes": "inesigibile"},
        )

        assert resp.status_code == 200
        test_db_session.expire_all()
        test_db_session.refresh(case)
        assert case.status == "closed"
        assert case.closed_reason == "archived"
        test_db_session.refresh(cust)
        assert cust.recovery_status == "archived"

    def test_note_never_blocked(self, test_client, test_db_session, overdue_customer):
        cust, invoices = overdue_customer
        test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [invoices[0].id], "channel": "whatsapp_copy"},
        )
        resp = test_client.post(
            f"/api/recovery/customers/{cust.id}/actions",
            json={"action_type": "note", "scheduled_date": None, "notes": "promette bonifico"},
        )
        assert resp.status_code == 200


class TestCustomerDetailCase:
    def test_detail_exposes_case_block(self, test_client, test_db_session, overdue_customer):
        cust, invoices = overdue_customer
        test_client.post(
            f"/api/recovery/customers/{cust.id}/solleciti",
            json={"invoice_ids": [invoices[0].id], "channel": "whatsapp_copy"},
        )

        resp = test_client.get(f"/api/customers/{cust.id}")
        data = resp.json()

        assert data["case"] is not None
        assert data["case"]["contact_count"] == 1
        assert data["case"]["sollecito_registered_today"] is True
        assert data["contact_action_count"] == 1
        # provenienza scadenza esposta per fattura
        assert all("due_date_source" in inv for inv in data["invoices"]["items"])
