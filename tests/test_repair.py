"""Test del repair pass degli abbinamenti (backend/engine/repair.py).

Il repair corregge retroattivamente gli errori del vecchio motore:
- detach deterministico su contraddizione P.IVA + riabbinamento sicuro
- promozione delle 'legacy' su cui il motore nuovo concorda
- relink via P.IVA certa per le 'legacy' attaccate a un cliente senza P.IVA
- decisioni umane (manual/fuzzy_confirmed/unlinked) mai toccate
- riconciliazione di pratiche e stato-cache dei clienti che perdono fatture
- idempotenza via marker SyncState 'match_repair_v1'
"""

from datetime import date, datetime

from backend.database import (
    Invoice, Customer, RecoveryCase, RecoveryAction, ActivityLog, SyncState,
)
from backend.engine.repair import repair_matches, REPAIR_MARKER_KEY

# P.IVA italiane checksum-valide
PIVA_QOQA = "12345678903"
PIVA_ROOFTOP = "98765432103"


def _customer(session, name, piva=None, **kw):
    c = Customer(ragione_sociale=name, partita_iva=piva, source="shopify", **kw)
    session.add(c)
    session.commit()
    return c


def _invoice(session, number, customer=None, **kw):
    inv = Invoice(
        invoice_number=number,
        amount=kw.pop("amount", 500.0),
        amount_due=kw.pop("amount_due", 500.0),
        issue_date=kw.pop("issue_date", date(2026, 4, 1)),
        due_date=kw.pop("due_date", date(2026, 5, 1)),
        days_overdue=kw.pop("days_overdue", 30),
        source_platform="fatturapro",
        status=kw.pop("status", "open"),
        customer_id=customer.id if customer else None,
        **kw,
    )
    session.add(inv)
    session.commit()
    return inv


class TestDetachOnPivaConflict:
    def test_detached_and_rematched_to_right_customer(self, test_db_session):
        """Il caso QOQA/Rooftop: fattura con P.IVA di QOQA attaccata a
        Rooftop (che ha un'ALTRA P.IVA valida) → detach + riabbinamento
        automatico a QOQA via Strategia 1."""
        rooftop = _customer(test_db_session, "Rooftop SRL", PIVA_ROOFTOP)
        qoqa = _customer(test_db_session, "QOQA SRL", PIVA_QOQA)
        inv = _invoice(
            test_db_session, "993/2026", customer=rooftop,
            customer_name_raw="QOQA SRL", customer_piva_raw=PIVA_QOQA,
            match_method="legacy",
        )

        stats = repair_matches(test_db_session)

        assert stats["piva_conflict_detached"] == 1
        test_db_session.refresh(inv)
        assert inv.customer_id == qoqa.id
        assert inv.match_method == "piva"
        log = test_db_session.query(ActivityLog).filter_by(
            action="repair_piva_conflict"
        ).all()
        assert len(log) == 1
        assert log[0].details["old_customer_name"] == "Rooftop SRL"

    def test_human_decisions_untouched(self, test_db_session):
        rooftop = _customer(test_db_session, "Rooftop SRL", PIVA_ROOFTOP)
        _customer(test_db_session, "QOQA SRL", PIVA_QOQA)
        for method, number in (
            ("manual", "M/2026"),
            ("fuzzy_confirmed", "F/2026"),
            ("unlinked", "U/2026"),
        ):
            _invoice(
                test_db_session, number, customer=rooftop,
                customer_piva_raw=PIVA_QOQA, match_method=method,
            )

        stats = repair_matches(test_db_session)

        assert stats["piva_conflict_detached"] == 0
        for number in ("M/2026", "F/2026", "U/2026"):
            inv = test_db_session.query(Invoice).filter_by(invoice_number=number).one()
            assert inv.customer_id == rooftop.id

    def test_paid_invoices_not_detached(self, test_db_session):
        """Il repair v1 lavora sulle non pagate (le pagate emergono
        dall'audit con include_paid e si trattano a mano)."""
        rooftop = _customer(test_db_session, "Rooftop SRL", PIVA_ROOFTOP)
        _customer(test_db_session, "QOQA SRL", PIVA_QOQA)
        inv = _invoice(
            test_db_session, "PAID/2026", customer=rooftop, status="paid",
            customer_piva_raw=PIVA_QOQA, match_method="legacy",
        )
        stats = repair_matches(test_db_session)
        assert stats["piva_conflict_detached"] == 0
        test_db_session.refresh(inv)
        assert inv.customer_id == rooftop.id


class TestLegacyRematch:
    def test_agreement_promotes_provenance(self, test_db_session):
        qoqa = _customer(test_db_session, "QOQA SRL", PIVA_QOQA)
        inv = _invoice(
            test_db_session, "1/2026", customer=qoqa,
            customer_name_raw="QOQA SRL", customer_piva_raw=PIVA_QOQA,
            match_method="legacy",
        )
        stats = repair_matches(test_db_session)
        assert stats["legacy_promoted"] == 1
        test_db_session.refresh(inv)
        assert inv.customer_id == qoqa.id
        assert inv.match_method == "piva"

    def test_piva_certainty_on_other_customer_relinks(self, test_db_session):
        """Legacy attaccata a un cliente SENZA P.IVA, ma la P.IVA della
        fattura appartiene con certezza a un altro cliente → relink."""
        izakaya = _customer(test_db_session, "iZAKAYA8", None)
        battiato = _customer(test_db_session, "Battiato Loris", PIVA_QOQA)
        inv = _invoice(
            test_db_session, "1077/2026", customer=izakaya,
            customer_name_raw="Battiato Loris", customer_piva_raw=PIVA_QOQA,
            match_method="legacy",
        )
        stats = repair_matches(test_db_session)
        assert stats["legacy_piva_relink_detached"] == 1
        test_db_session.refresh(inv)
        assert inv.customer_id == battiato.id
        assert inv.match_method == "piva"

    def test_uncertain_disagreement_only_logged(self, test_db_session):
        """Senza certezza P.IVA il repair non tocca nulla: solo un
        ActivityLog di review."""
        izakaya = _customer(test_db_session, "iZAKAYA8", None)
        _customer(test_db_session, "Battiato Loris & Co.", None)
        inv = _invoice(
            test_db_session, "1078/2026", customer=izakaya,
            customer_name_raw="Batiato Loris e Co", match_method="legacy",
        )
        stats = repair_matches(test_db_session)
        assert stats["legacy_piva_relink_detached"] == 0
        test_db_session.refresh(inv)
        assert inv.customer_id == izakaya.id
        assert inv.match_method == "legacy"
        if stats["legacy_review_logged"]:
            log = test_db_session.query(ActivityLog).filter_by(
                action="repair_legacy_review"
            ).all()
            assert len(log) == stats["legacy_review_logged"]


class TestReconciliation:
    def test_emptied_case_closed_and_status_reset(self, test_db_session):
        rooftop = _customer(test_db_session, "Rooftop SRL", PIVA_ROOFTOP)
        rooftop.recovery_status = "first_contact"
        qoqa = _customer(test_db_session, "QOQA SRL", PIVA_QOQA)
        case = RecoveryCase(customer_id=rooftop.id, status="open", opened_at=datetime.utcnow())
        test_db_session.add(case)
        test_db_session.commit()
        inv = _invoice(
            test_db_session, "993/2026", customer=rooftop,
            customer_piva_raw=PIVA_QOQA, match_method="legacy", case_id=case.id,
        )
        todo = RecoveryAction(
            customer_id=rooftop.id, case_id=case.id,
            action_type="second_contact", scheduled_date=date(2026, 8, 1),
        )
        test_db_session.add(todo)
        test_db_session.commit()

        stats = repair_matches(test_db_session)

        assert stats["cases_closed"] == 1
        test_db_session.refresh(case)
        test_db_session.refresh(rooftop)
        test_db_session.refresh(todo)
        test_db_session.refresh(inv)
        assert case.status == "closed"
        assert case.closed_reason == "no_overdue"
        assert rooftop.recovery_status == "idle"
        assert todo.cancelled is True
        assert inv.customer_id == qoqa.id
        assert inv.case_id is None

    def test_case_with_own_invoices_stays_open(self, test_db_session):
        rooftop = _customer(test_db_session, "Rooftop SRL", PIVA_ROOFTOP)
        _customer(test_db_session, "QOQA SRL", PIVA_QOQA)
        case = RecoveryCase(customer_id=rooftop.id, status="open", opened_at=datetime.utcnow())
        test_db_session.add(case)
        test_db_session.commit()
        # Fattura sbagliata (di QOQA) + fattura PROPRIA di Rooftop, entrambe scadute
        _invoice(
            test_db_session, "993/2026", customer=rooftop,
            customer_piva_raw=PIVA_QOQA, match_method="legacy", case_id=case.id,
        )
        _invoice(
            test_db_session, "500/2026", customer=rooftop,
            customer_piva_raw=PIVA_ROOFTOP, match_method="piva", case_id=case.id,
        )

        stats = repair_matches(test_db_session)

        assert stats["piva_conflict_detached"] == 1
        test_db_session.refresh(case)
        assert case.status == "open"
        own = test_db_session.query(Invoice).filter_by(invoice_number="500/2026").one()
        assert own.customer_id == rooftop.id


class TestIdempotence:
    def test_second_run_skips(self, test_db_session):
        _customer(test_db_session, "Rooftop SRL", PIVA_ROOFTOP)
        stats1 = repair_matches(test_db_session)
        assert "skipped" not in stats1
        stats2 = repair_matches(test_db_session)
        assert stats2 == {"skipped": True}
        marker = test_db_session.query(SyncState).filter_by(key=REPAIR_MARKER_KEY).one()
        assert marker.result["done"] is True

    def test_no_customers_no_crash(self, test_db_session):
        stats = repair_matches(test_db_session)
        assert stats["piva_conflict_detached"] == 0
