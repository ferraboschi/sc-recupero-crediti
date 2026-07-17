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

    def test_archived_case_is_not_reopened_by_next_sync(self, test_db_session):
        """Archiviare = 'questo debito non lo inseguo più'. Le fatture scadute
        restano scadute per definizione: il lifecycle successivo NON deve aprire
        una pratica nuova sulle STESSE fatture né riportare il cliente a 'idle'.

        Regressione: il pulsante Archivia era un no-op che durava fino al sync
        successivo, e il debitore inesigibile ricompariva fra i 'da contattare
        per la prima volta' (dashboard.py:167-183).
        """
        cust = make_customer(test_db_session, name="Debitore Cronico SRL")
        inv = make_invoice(test_db_session, cust, number="OLD-1")
        update_case_lifecycle(test_db_session)
        case = get_open_case(test_db_session, cust.id)
        assert case is not None
        assert inv.case_id == case.id

        close_case(test_db_session, case, "archived")
        test_db_session.commit()
        assert cust.recovery_status == "archived"

        # il sync successivo: nessun pagamento, nessuna azione operatore
        stats = update_case_lifecycle(test_db_session)

        assert stats["opened"] == 0
        assert stats["reopened"] == 0
        assert get_open_case(test_db_session, cust.id) is None
        test_db_session.refresh(cust)
        assert cust.recovery_status == "archived"
        test_db_session.refresh(inv)
        assert inv.case_id == case.id  # la fattura resta nella pratica archiviata

    def test_archived_debt_partially_paid_stays_archived(self, test_db_session):
        """Il saldo di UNA fattura del ciclo archiviato non resuscita il resto:
        l'archiviazione copriva l'intero ciclo, non la singola fattura."""
        cust = make_customer(test_db_session, name="Debitore Cronico SRL")
        inv_a = make_invoice(test_db_session, cust, number="ARC-1")
        make_invoice(test_db_session, cust, number="ARC-2")
        update_case_lifecycle(test_db_session)
        case = get_open_case(test_db_session, cust.id)
        close_case(test_db_session, case, "archived")

        inv_a.status = "paid"
        inv_a.amount_due = 0
        inv_a.days_overdue = 0
        test_db_session.commit()

        update_case_lifecycle(test_db_session)

        assert get_open_case(test_db_session, cust.id) is None
        test_db_session.refresh(cust)
        assert cust.recovery_status == "archived"

    def test_new_debt_opens_case_without_resurrecting_archived_debt(self, test_db_session):
        """Un debito NUOVO merita una pratica nuova — ma il debito archiviato
        NON la segue: resta nella pratica archiviata. Altrimenti la pratica
        nuova nascerebbe con dentro una fattura inesigibile e non potrebbe mai
        chiudersi a saldo.

        Copre il buco di test_case_after_archive_inherits_contacts, che salda
        la vecchia fattura e quindi non distingue le due politiche.
        """
        cust = make_customer(test_db_session, name="Debitore Cronico SRL")
        old_inv = make_invoice(test_db_session, cust, number="ARC-1")
        update_case_lifecycle(test_db_session)
        archived = get_open_case(test_db_session, cust.id)
        close_case(test_db_session, archived, "archived")
        test_db_session.commit()

        # arriva debito NUOVO, mentre ARC-1 resta scaduta e non pagata
        new_inv = make_invoice(test_db_session, cust, number="NEW-1", days_overdue=10)
        stats = update_case_lifecycle(test_db_session)

        assert stats["opened"] == 1
        new_case = get_open_case(test_db_session, cust.id)
        assert new_case is not None and new_case.id != archived.id
        test_db_session.refresh(old_inv)
        test_db_session.refresh(new_inv)
        assert old_inv.case_id == archived.id  # l'inesigibile non risorge
        assert new_inv.case_id == new_case.id

        # e la pratica nuova può chiudersi a saldo: non trascina ARC-1
        new_inv.status = "paid"
        new_inv.amount_due = 0
        new_inv.days_overdue = 0
        test_db_session.commit()
        update_case_lifecycle(test_db_session)

        test_db_session.refresh(new_case)
        assert new_case.status == "closed"
        assert new_case.closed_reason == "paid"

    def test_reopen_never_restarts_tone_after_archive(self, test_db_session):
        """Dopo un'archiviazione il tono non riparte mai cordiale.

        Regressione: _find_reopenable_case scavalcava la pratica archiviata e
        riapriva una 'no_overdue' antecedente col contatore a zero, così il
        cliente appena passato all'avvocato riceveva un primo sollecito
        cordiale — l'invariante di database.py (inherited_contacts) violato.
        """
        cust = make_customer(test_db_session, name="Debitore Cronico SRL")

        # 1. Fattura X scaduta → pratica 1; la scadenza viene corretta nel
        #    futuro → pratica 1 chiusa 'no_overdue', X le resta agganciata.
        inv_x = make_invoice(test_db_session, cust, number="X-1")
        update_case_lifecycle(test_db_session)
        case_1 = get_open_case(test_db_session, cust.id)
        assert inv_x.case_id == case_1.id
        inv_x.due_date = date.today() + timedelta(days=30)
        inv_x.days_overdue = -30
        test_db_session.commit()
        update_case_lifecycle(test_db_session)
        test_db_session.refresh(case_1)
        assert case_1.closed_reason == "no_overdue"
        assert inv_x.case_id == case_1.id
        # la chiusura 'no_overdue' è PRECEDENTE all'archiviazione che segue
        case_1.closed_at = datetime.utcnow() - timedelta(days=10)
        test_db_session.commit()

        # 2. Fattura Y scaduta → pratica 2, due contatti, poi l'operatore archivia.
        inv_y = make_invoice(test_db_session, cust, number="Y-1", days_overdue=25)
        update_case_lifecycle(test_db_session)
        case_2 = get_open_case(test_db_session, cust.id)
        assert case_2.id != case_1.id
        assert inv_y.case_id == case_2.id
        for kind in ("first_contact", "second_contact"):
            test_db_session.add(RecoveryAction(
                customer_id=cust.id, case_id=case_2.id, action_type=kind,
                scheduled_date=date.today(), completed_at=datetime.utcnow(),
            ))
        test_db_session.commit()
        close_case(test_db_session, case_2, "archived")
        test_db_session.commit()
        assert contact_count(test_db_session, case_2) == 2

        # 3. Arriva la scadenza VERA di X, mentre Y resta insoluta e archiviata.
        inv_x.due_date = date.today() - timedelta(days=5)
        inv_x.days_overdue = 5
        test_db_session.commit()

        stats = update_case_lifecycle(test_db_session)

        # La pratica archiviata è la decisione più recente dell'operatore:
        # non si scavalca per riaprire la 'no_overdue' antecedente.
        test_db_session.refresh(case_1)
        assert case_1.status == "closed"
        assert stats["reopened"] == 0
        assert stats["opened"] == 1

        new_case = get_open_case(test_db_session, cust.id)
        assert new_case is not None
        assert new_case.id not in (case_1.id, case_2.id)
        assert new_case.reopened_after_archive is True
        assert new_case.inherited_contacts == 2
        assert contact_count(test_db_session, new_case) == 2  # il tono NON riparte

        # X è debito nuovamente esigibile → passa alla pratica nuova;
        # Y resta inesigibile nella pratica archiviata.
        test_db_session.refresh(inv_x)
        test_db_session.refresh(inv_y)
        assert inv_x.case_id == new_case.id
        assert inv_y.case_id == case_2.id

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


class TestAuditFixes:
    """Regressioni dei difetti confermati dall'audit avversario finale."""

    def test_backfill_reuses_existing_open_case(self, test_db_session):
        """Backfill riavviabile: se una pratica aperta esiste già (lifecycle
        partito prima, o retry dopo fallimento a metà) la riusa senza
        violare l'indice UNIQUE."""
        cust = make_customer(test_db_session)
        make_invoice(test_db_session, cust, days_overdue=15)
        # il lifecycle è passato PRIMA del backfill (startup-sync)
        update_case_lifecycle(test_db_session)
        existing = get_open_case(test_db_session, cust.id)
        assert existing is not None

        stats = backfill_cases(test_db_session)

        assert stats.get("skipped") is not True
        assert stats["cases_created"] == 0  # riusata, non duplicata
        assert test_db_session.query(RecoveryCase).filter_by(
            customer_id=cust.id, status="open"
        ).count() == 1
        marker = test_db_session.query(SyncState).filter_by(key="case_backfill").first()
        assert marker.result["done"] is True

    def test_open_new_case_conflict_does_not_discard_session(self, test_db_session):
        """Il conflitto sull'indice UNIQUE non deve scartare il lavoro non
        committato del pass (SAVEPOINT, non rollback di sessione)."""
        cust_a = make_customer(test_db_session, name="Cliente A SRL")
        cust_b = make_customer(test_db_session, name="Cliente B SRL")
        make_invoice(test_db_session, cust_b, number="B-1")
        # pratica già aperta per A
        open_new_case(test_db_session, cust_a)
        test_db_session.commit()

        # lavoro non committato nel pass corrente: pratica per B
        case_b = open_new_case(test_db_session, cust_b)
        # conflitto: seconda apertura per A → deve riusare, senza buttare B
        reused = open_new_case(test_db_session, cust_a)
        test_db_session.commit()

        assert reused.id is not None
        assert test_db_session.query(RecoveryCase).filter_by(
            customer_id=cust_b.id, status="open"
        ).count() == 1
        assert test_db_session.query(RecoveryCase).filter_by(
            customer_id=cust_a.id, status="open"
        ).count() == 1


class TestBusinessDay:
    def test_business_day_start_is_rome_midnight_in_utc(self):
        from zoneinfo import ZoneInfo
        from datetime import timezone as tz
        from backend.engine.cases import business_day_start
        from backend.config import config

        fixed = datetime(2026, 7, 14, 23, 30)  # 23:30 UTC = 01:30 del 15/07 a Roma (estate)
        start = business_day_start(fixed)
        rome = ZoneInfo(config.TIMEZONE)
        expected_local = datetime(2026, 7, 15, 0, 0, tzinfo=rome)
        assert start == expected_local.astimezone(tz.utc).replace(tzinfo=None)
        # e per un istante di giorno pieno, la mezzanotte italiana dello stesso giorno
        fixed2 = datetime(2026, 7, 14, 10, 0)
        start2 = business_day_start(fixed2)
        expected2 = datetime(2026, 7, 14, 0, 0, tzinfo=rome)
        assert start2 == expected2.astimezone(tz.utc).replace(tzinfo=None)
