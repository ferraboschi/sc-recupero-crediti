"""Test lifecycle pratiche di recupero (RecoveryCase) e backfill."""

from datetime import datetime, date, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryCase, RecoveryAction, SyncState
from backend.engine.cases import (
    update_case_lifecycle, backfill_cases, get_open_case, contact_count,
    close_case, open_new_case, schedule_next_action,
)


def make_customer(session, name="ACME S.R.L.", **kw):
    c = Customer(ragione_sociale=name, **kw)
    session.add(c)
    session.commit()
    return c


def make_invoice(session, customer, number="INV001", days_overdue=20, status="open", **kw):
    today = date.today()
    inv = Invoice(
        invoice_number=number,
        amount=1000.0,
        amount_due=1000.0 if status != "paid" else 0.0,
        issue_date=today - timedelta(days=days_overdue + 30),
        due_date=today - timedelta(days=days_overdue),
        days_overdue=days_overdue,
        status=status,
        customer_id=customer.id,
        source_platform="fatturapro",
        **kw,
    )
    session.add(inv)
    session.commit()
    return inv


class TestLifecycleApertura:
    def test_opens_case_for_overdue_customer(self, test_db_session):
        cust = make_customer(test_db_session)
        inv = make_invoice(test_db_session, cust)

        stats = update_case_lifecycle(test_db_session)

        assert stats["opened"] == 1
        case = get_open_case(test_db_session, cust.id)
        assert case is not None
        test_db_session.refresh(inv)
        assert inv.case_id == case.id

    def test_no_case_without_overdue(self, test_db_session):
        cust = make_customer(test_db_session)
        make_invoice(test_db_session, cust, days_overdue=-10)  # scade in futuro

        stats = update_case_lifecycle(test_db_session)

        assert stats["opened"] == 0
        assert get_open_case(test_db_session, cust.id) is None

    def test_disputed_does_not_open_case(self, test_db_session):
        cust = make_customer(test_db_session)
        make_invoice(test_db_session, cust, status="disputed")

        update_case_lifecycle(test_db_session)
        assert get_open_case(test_db_session, cust.id) is None

    def test_excluded_customer_case_closed(self, test_db_session):
        cust = make_customer(test_db_session, excluded=True)
        make_invoice(test_db_session, cust)
        # pratica esistente (aperta prima dell'esclusione)
        case = open_new_case(test_db_session, cust)
        test_db_session.commit()

        update_case_lifecycle(test_db_session)

        test_db_session.refresh(case)
        assert case.status == "closed"
        assert case.closed_reason == "excluded"


class TestLifecycleChiusura:
    def test_paid_closes_case_and_resets_customer(self, test_db_session):
        cust = make_customer(test_db_session)
        inv = make_invoice(test_db_session, cust)
        update_case_lifecycle(test_db_session)
        case = get_open_case(test_db_session, cust.id)

        # todo pendente della pratica
        pending = RecoveryAction(
            customer_id=cust.id, case_id=case.id,
            action_type="second_contact", scheduled_date=date.today() + timedelta(days=7),
        )
        test_db_session.add(pending)
        cust.recovery_status = "first_contact"
        test_db_session.commit()

        # saldo completo
        inv.status = "paid"
        inv.amount_due = 0
        inv.days_overdue = 0
        test_db_session.commit()

        stats = update_case_lifecycle(test_db_session)

        assert stats["closed"] == 1
        test_db_session.refresh(case)
        test_db_session.refresh(cust)
        test_db_session.refresh(pending)
        assert case.status == "closed"
        assert case.closed_reason == "paid"
        assert cust.recovery_status == "idle"
        assert cust.next_action_date is None
        assert pending.cancelled is True
        assert pending.cancelled_reason == "case_closed"

    def test_no_overdue_close_when_due_date_slips_future(self, test_db_session):
        """Scadenza corretta nel futuro: chiusura 'no_overdue', MAI 'paid'."""
        cust = make_customer(test_db_session)
        inv = make_invoice(test_db_session, cust)
        update_case_lifecycle(test_db_session)
        case = get_open_case(test_db_session, cust.id)

        # la scadenza reale arriva dal gestionale: è tra 20 giorni
        inv.due_date = date.today() + timedelta(days=20)
        inv.days_overdue = -20
        test_db_session.commit()

        update_case_lifecycle(test_db_session)

        test_db_session.refresh(case)
        assert case.status == "closed"
        assert case.closed_reason == "no_overdue"  # NON 'paid': nulla è stato pagato

    def test_allow_close_false_never_closes(self, test_db_session):
        """Fetch parziale: le chiusure sono sospese (payment detection inaffidabile)."""
        cust = make_customer(test_db_session)
        inv = make_invoice(test_db_session, cust)
        update_case_lifecycle(test_db_session)
        inv.status = "paid"
        inv.amount_due = 0
        inv.days_overdue = 0
        test_db_session.commit()

        stats = update_case_lifecycle(test_db_session, allow_close=False)

        assert stats["closed"] == 0
        case = get_open_case(test_db_session, cust.id)
        assert case is not None

    def test_only_disputed_left_closes_resolved(self, test_db_session):
        cust = make_customer(test_db_session)
        inv = make_invoice(test_db_session, cust)
        update_case_lifecycle(test_db_session)

        inv.status = "disputed"
        test_db_session.commit()
        update_case_lifecycle(test_db_session)

        case = test_db_session.query(RecoveryCase).filter_by(customer_id=cust.id).first()
        assert case.status == "closed"
        assert case.closed_reason == "resolved"


class TestLifecycleRiapertura:
    def test_paid_flapping_reopens_same_case(self, test_db_session):
        """Anti-flapping: la fattura 'ripagata' per errore dello scraper che
        ricompare entro 30gg riapre la STESSA pratica (contatore intatto)."""
        cust = make_customer(test_db_session)
        inv = make_invoice(test_db_session, cust)
        update_case_lifecycle(test_db_session)
        case = get_open_case(test_db_session, cust.id)

        # un contatto già fatto
        test_db_session.add(RecoveryAction(
            customer_id=cust.id, case_id=case.id, action_type="first_contact",
            scheduled_date=date.today(), completed_at=datetime.utcnow(),
        ))
        # falso pagamento (fetch parziale sfuggito) → chiusura
        inv.status = "paid"
        inv.amount_due = 0
        inv.days_overdue = 0
        test_db_session.commit()
        update_case_lifecycle(test_db_session)
        test_db_session.refresh(case)
        assert case.status == "closed"

        # la fattura ricompare scaduta
        inv.status = "open"
        inv.amount_due = 1000.0
        inv.days_overdue = 21
        test_db_session.commit()
        stats = update_case_lifecycle(test_db_session)

        assert stats["reopened"] == 1
        assert stats["opened"] == 0
        test_db_session.refresh(case)
        assert case.status == "open"
        assert contact_count(test_db_session, case) == 1  # contatore preservato

    def test_new_invoice_after_real_payment_opens_fresh_case(self, test_db_session):
        """Il fix del punto 3: dopo un saldo VERO, una fattura NUOVA apre una
        pratica nuova e la numerazione riparte da zero."""
        cust = make_customer(test_db_session)
        inv = make_invoice(test_db_session, cust, number="OLD-1")
        update_case_lifecycle(test_db_session)
        old_case = get_open_case(test_db_session, cust.id)
        test_db_session.add(RecoveryAction(
            customer_id=cust.id, case_id=old_case.id, action_type="second_contact",
            scheduled_date=date.today(), completed_at=datetime.utcnow(),
        ))
        inv.status = "paid"
        inv.amount_due = 0
        inv.days_overdue = 0
        test_db_session.commit()
        update_case_lifecycle(test_db_session)
        # chiusura avvenuta oltre la finestra anti-flapping
        old_case.closed_at = datetime.utcnow() - timedelta(days=45)
        test_db_session.commit()

        make_invoice(test_db_session, cust, number="NEW-1", days_overdue=8)
        stats = update_case_lifecycle(test_db_session)

        assert stats["opened"] == 1
        new_case = get_open_case(test_db_session, cust.id)
        assert new_case.id != old_case.id
        assert contact_count(test_db_session, new_case) == 0  # riparte da PRIMA

    def test_case_after_archive_inherits_contacts(self, test_db_session):
        """Dopo una pratica archiviata il tono NON riparte cordiale."""
        cust = make_customer(test_db_session)
        inv = make_invoice(test_db_session, cust, number="OLD-1")
        update_case_lifecycle(test_db_session)
        old_case = get_open_case(test_db_session, cust.id)
        test_db_session.add(RecoveryAction(
            customer_id=cust.id, case_id=old_case.id, action_type="first_contact",
            scheduled_date=date.today(), completed_at=datetime.utcnow(),
        ))
        test_db_session.add(RecoveryAction(
            customer_id=cust.id, case_id=old_case.id, action_type="second_contact",
            scheduled_date=date.today(), completed_at=datetime.utcnow(),
        ))
        test_db_session.commit()
        close_case(test_db_session, old_case, "archived")
        # la vecchia fattura è della pratica archiviata; ne arriva una nuova
        inv.status = "paid"
        inv.amount_due = 0
        inv.days_overdue = 0
        make_invoice(test_db_session, cust, number="NEW-1", days_overdue=10)
        test_db_session.commit()

        update_case_lifecycle(test_db_session)

        new_case = get_open_case(test_db_session, cust.id)
        assert new_case is not None
        assert new_case.id != old_case.id
        assert new_case.reopened_after_archive is True
        assert new_case.inherited_contacts == 2
        assert contact_count(test_db_session, new_case) == 2

    def test_reassigned_invoice_detached_from_foreign_case(self, test_db_session):
        """Regola difensiva: fattura riassegnata non resta nella pratica altrui."""
        cust_a = make_customer(test_db_session, name="Cliente A SRL")
        cust_b = make_customer(test_db_session, name="Cliente B SRL")
        inv = make_invoice(test_db_session, cust_a)
        update_case_lifecycle(test_db_session)
        case_a = get_open_case(test_db_session, cust_a.id)
        assert inv.case_id == case_a.id

        # riassegnazione "grezza" (senza pulizia case_id)
        inv.customer_id = cust_b.id
        test_db_session.commit()

        stats = update_case_lifecycle(test_db_session)

        test_db_session.refresh(inv)
        case_b = get_open_case(test_db_session, cust_b.id)
        assert stats["detached"] >= 1
        assert inv.case_id == case_b.id


class TestProgression:
    def test_schedule_next_after_first_contact(self, test_db_session):
        cust = make_customer(test_db_session)
        make_invoice(test_db_session, cust)
        case = open_new_case(test_db_session, cust)
        test_db_session.commit()

        nxt = schedule_next_action(test_db_session, cust, case, contacts_done=1)

        assert nxt.action_type == "second_contact"
        assert nxt.scheduled_date == date.today() + timedelta(days=7)
        assert cust.next_action_type == "second_contact"

    def test_schedule_next_after_second_contact_is_lawyer(self, test_db_session):
        cust = make_customer(test_db_session)
        make_invoice(test_db_session, cust)
        case = open_new_case(test_db_session, cust)
        test_db_session.commit()

        nxt = schedule_next_action(test_db_session, cust, case, contacts_done=2)

        assert nxt.action_type == "lawyer"
        assert nxt.scheduled_date == date.today() + timedelta(days=14)

    def test_pending_lawyer_blocks_new_scheduling(self, test_db_session):
        cust = make_customer(test_db_session)
        make_invoice(test_db_session, cust)
        case = open_new_case(test_db_session, cust)
        lawyer_todo = RecoveryAction(
            customer_id=cust.id, case_id=case.id, action_type="lawyer",
            scheduled_date=date.today() + timedelta(days=10),
        )
        test_db_session.add(lawyer_todo)
        test_db_session.commit()

        nxt = schedule_next_action(test_db_session, cust, case, contacts_done=1)

        assert nxt is None  # il todo legale resta l'unico next step
        assert cust.next_action_type == "lawyer"


class TestBackfill:
    def test_backfill_creates_cases_and_converts_pending_contacts(self, test_db_session):
        cust = make_customer(test_db_session)
        make_invoice(test_db_session, cust, days_overdue=15)
        # azione legacy: contatto registrato 10 giorni fa (pendente by design),
        # con ricontatto pianificato tra 4 giorni
        legacy = RecoveryAction(
            customer_id=cust.id, action_type="first_contact",
            scheduled_date=date.today() + timedelta(days=4),
        )
        legacy.created_at = datetime.utcnow() - timedelta(days=10)
        test_db_session.add(legacy)
        cust.recovery_status = "first_contact"
        test_db_session.commit()

        stats = backfill_cases(test_db_session)

        assert stats["cases_created"] == 1
        assert stats["pending_converted"] == 1
        case = get_open_case(test_db_session, cust.id)
        test_db_session.refresh(legacy)
        # il contatto legacy risulta ESEGUITO alla data in cui fu registrato
        assert legacy.completed_at is not None
        assert legacy.completed_at.date() == (datetime.utcnow() - timedelta(days=10)).date()
        assert legacy.case_id == case.id
        assert contact_count(test_db_session, case) == 1
        # il todo futuro è stato ricreato come pending del tipo successivo
        recreated = test_db_session.query(RecoveryAction).filter(
            RecoveryAction.case_id == case.id,
            RecoveryAction.completed_at.is_(None),
        ).all()
        assert len(recreated) == 1
        assert recreated[0].action_type == "second_contact"
        assert recreated[0].scheduled_date == date.today() + timedelta(days=4)

    def test_backfill_resets_stale_customers(self, test_db_session):
        """Cliente 'in recupero' ma senza scadute: lo stato stantio si resetta."""
        cust = make_customer(test_db_session)
        make_invoice(test_db_session, cust, status="paid", days_overdue=0)
        cust.recovery_status = "second_contact"
        cust.next_action_date = date.today() + timedelta(days=3)
        orphan_todo = RecoveryAction(
            customer_id=cust.id, action_type="second_contact",
            scheduled_date=date.today() + timedelta(days=3),
        )
        test_db_session.add(orphan_todo)
        test_db_session.commit()

        stats = backfill_cases(test_db_session)

        assert stats["stale_customers_reset"] == 1
        test_db_session.refresh(cust)
        test_db_session.refresh(orphan_todo)
        assert cust.recovery_status == "idle"
        assert cust.next_action_date is None
        assert orphan_todo.cancelled is True

    def test_backfill_idempotent_via_marker(self, test_db_session):
        cust = make_customer(test_db_session)
        make_invoice(test_db_session, cust)

        first = backfill_cases(test_db_session)
        second = backfill_cases(test_db_session)

        assert first["cases_created"] == 1
        assert second == {"skipped": True}
        marker = test_db_session.query(SyncState).filter_by(key="case_backfill").first()
        assert marker is not None
        assert marker.result["done"] is True
        # nessuna pratica duplicata
        assert test_db_session.query(RecoveryCase).count() == 1
