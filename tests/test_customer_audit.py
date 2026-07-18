"""Test dell'audit abbinamenti PER-CLIENTE + filtri/ordinamenti della lista.

Copertura:
- GET /api/customers/{id}/audit: audit del singolo cliente (scoped alle sue
  fatture, niente scansione globale), livelli warn/bad, reviewed, include_paid,
  can_assign_piva, suggerimenti pendenti.
- GET /api/customers/audit-summary: conteggio "da sanificare" (dedup + pendenti).
- GET /api/customers: nuovi filtri (to_sanitize, no_phone, recovery_status) e
  ordinamenti (days_overdue, last_action) + campi max_days_overdue/last_action.
"""

from datetime import date, datetime, timedelta

from backend.database import Customer, Invoice, RecoveryAction

# P.IVA italiane checksum-valide (vedi backend/engine/piva.py)
PIVA_A = "12345678903"
PIVA_B = "98765432103"


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


# ── GET /customers/{id}/audit ────────────────────────────────────────

class TestCustomerAudit:
    def test_scoped_to_single_customer(self, test_client, test_db_session):
        # Due clienti, ciascuno con una fattura problematica. L'audit del primo
        # NON deve vedere la fattura del secondo.
        c1 = _customer(test_db_session, "Rooftop SRL", partita_iva=PIVA_A)
        c2 = _customer(test_db_session, "Altro SRL", partita_iva=PIVA_A)
        _invoice(
            test_db_session, "993/2026", customer_id=c1.id,
            customer_name_raw="QOQA di Amanda", customer_piva_raw=PIVA_A,
        )
        _invoice(
            test_db_session, "994/2026", customer_id=c2.id,
            customer_name_raw="Pinco Pallino", customer_piva_raw=PIVA_A,
        )
        data = test_client.get(f"/api/customers/{c1.id}/audit").json()
        assert data["total_invoices"] == 1
        assert data["problem_count"] == 1
        assert data["items"][0]["invoice_number"] == "993/2026"

    def test_poisoned_piva_flagged_bad(self, test_client, test_db_session):
        # P.IVA coincidente ma nomi dissimili: critico (possibile avvelenata).
        c = _customer(test_db_session, "Rooftop SRL", partita_iva=PIVA_A)
        _invoice(
            test_db_session, "993/2026", customer_id=c.id,
            customer_name_raw="QOQA di Amanda Piccolo", customer_piva_raw=PIVA_A,
        )
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert data["worst_verdict"] == "bad"
        assert data["counts"]["bad"] == 1
        item = data["items"][0]
        assert item["verdict"] == "bad"
        assert item["verification"]["level"] == "critical"
        assert "avvelenata" in item["verification"]["message"]

    def test_clean_customer_has_no_problems(self, test_client, test_db_session):
        c = _customer(test_db_session, "Rossi SRL", partita_iva=PIVA_A)
        _invoice(
            test_db_session, "R1/2026", customer_id=c.id,
            customer_name_raw="Rossi SRL", customer_piva_raw=PIVA_A,
        )
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert data["problem_count"] == 0
        assert data["worst_verdict"] == "ok"
        assert data["items"] == []

    def test_404_for_missing_customer(self, test_client, test_db_session):
        assert test_client.get("/api/customers/9999/audit").status_code == 404

    def test_paid_excluded_unless_include_paid(self, test_client, test_db_session):
        c = _customer(test_db_session, "Belfiore M & M srl", partita_iva=PIVA_A)
        _invoice(
            test_db_session, "655/2026", customer_id=c.id, status="paid",
            customer_name_raw="Altra Azienda SRL", customer_piva_raw=PIVA_B,
        )
        # Senza include_paid la pagata è invisibile
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert data["total_invoices"] == 0
        assert data["problem_count"] == 0
        # Con include_paid emerge il conflitto P.IVA
        data = test_client.get(
            f"/api/customers/{c.id}/audit?include_paid=true"
        ).json()
        assert data["total_invoices"] == 1
        assert data["items"][0]["verdict"] == "bad"

    def test_reviewed_excluded_then_included(self, test_client, test_db_session):
        c = _customer(test_db_session, "Rossi SRL")
        inv = _invoice(
            test_db_session, "R2/2026", customer_id=c.id,
            customer_name_raw="Rossi SRL", customer_piva_raw=PIVA_A,
        )
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert data["problem_count"] == 1
        assert data["reviewed_count"] == 0
        # Segna verificato → esce dai problemi ma reviewed_count sale.
        assert test_client.post(f"/api/positions/{inv.id}/mark-reviewed").status_code == 200
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert data["problem_count"] == 0
        assert data["reviewed_count"] == 1
        # Con include_reviewed ricompare, marcata verificata.
        data = test_client.get(
            f"/api/customers/{c.id}/audit?include_reviewed=true"
        ).json()
        assert data["problem_count"] == 1
        assert data["items"][0]["reviewed"] is True

    def test_can_assign_piva(self, test_client, test_db_session):
        # Fattura con P.IVA valida, cliente senza: caso "copia la P.IVA".
        c = _customer(test_db_session, "Rossi SRL")  # nessuna P.IVA
        _invoice(
            test_db_session, "R3/2026", customer_id=c.id,
            customer_name_raw="Rossi SRL", customer_piva_raw=PIVA_A,
        )
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        item = data["items"][0]
        assert item["verdict"] == "warn"
        assert item["can_assign_piva"] is True

    def test_pending_suggestions_included(self, test_client, test_db_session):
        c = _customer(test_db_session, "Domò Milano", partita_iva=PIVA_A)
        # Fattura in quarantena suggerita a questo cliente
        _invoice(
            test_db_session, "Q1/2026", customer_id=None,
            suggested_customer_id=c.id, suggested_method="fuzzy",
            suggested_score=70, customer_name_raw="Domo Milano",
        )
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert data["pending_count"] == 1
        assert data["pending_suggestions"][0]["invoice_number"] == "Q1/2026"
        assert "verification" in data["pending_suggestions"][0]

    def test_after_unlink_problem_disappears(self, test_client, test_db_session):
        # Azione dall'audit (Scollega, endpoint esistente): ricaricando l'audit
        # il problema sparisce.
        c = _customer(test_db_session, "Rooftop SRL", partita_iva=PIVA_A)
        inv = _invoice(
            test_db_session, "993/2026", customer_id=c.id,
            customer_name_raw="QOQA di Amanda Piccolo", customer_piva_raw=PIVA_A,
        )
        assert test_client.get(f"/api/customers/{c.id}/audit").json()["problem_count"] == 1
        assert test_client.post(f"/api/positions/{inv.id}/unlink").status_code == 200
        assert test_client.get(f"/api/customers/{c.id}/audit").json()["problem_count"] == 0


# ── GET /customers/audit-summary ─────────────────────────────────────

class TestAuditSummary:
    def test_counts_customers_to_sanitize(self, test_client, test_db_session):
        bad = _customer(test_db_session, "Rooftop SRL", partita_iva=PIVA_A)
        _invoice(
            test_db_session, "993/2026", customer_id=bad.id,
            customer_name_raw="QOQA di Amanda", customer_piva_raw=PIVA_A,
        )
        clean = _customer(test_db_session, "Rossi SRL", partita_iva=PIVA_A)
        _invoice(
            test_db_session, "R1/2026", customer_id=clean.id,
            customer_name_raw="Rossi SRL", customer_piva_raw=PIVA_A,
        )
        data = test_client.get("/api/customers/audit-summary").json()
        assert data["to_sanitize_count"] == 1
        assert data["customer_ids"] == [bad.id]

    def test_pending_suggestion_counts_as_to_sanitize(self, test_client, test_db_session):
        c = _customer(test_db_session, "Domò Milano", partita_iva=PIVA_A)
        # cliente pulito sulle sue fatture, ma con un suggerimento pendente
        _invoice(
            test_db_session, "R1/2026", customer_id=c.id,
            customer_name_raw="Domò Milano", customer_piva_raw=PIVA_A,
        )
        _invoice(
            test_db_session, "Q1/2026", customer_id=None,
            suggested_customer_id=c.id, suggested_method="fuzzy", suggested_score=70,
            customer_name_raw="Domo Milano",
        )
        data = test_client.get("/api/customers/audit-summary").json()
        assert c.id in data["customer_ids"]

    def test_reviewed_not_counted(self, test_client, test_db_session):
        c = _customer(test_db_session, "Rossi SRL")
        inv = _invoice(
            test_db_session, "R2/2026", customer_id=c.id,
            customer_name_raw="Rossi SRL", customer_piva_raw=PIVA_A,
        )
        assert test_client.get("/api/customers/audit-summary").json()["to_sanitize_count"] == 1
        test_client.post(f"/api/positions/{inv.id}/mark-reviewed")
        assert test_client.get("/api/customers/audit-summary").json()["to_sanitize_count"] == 0


# ── GET /customers: nuovi filtri e ordinamenti ───────────────────────

class TestCustomerListFilters:
    def test_to_sanitize_filter(self, test_client, test_db_session):
        bad = _customer(test_db_session, "Rooftop SRL", partita_iva=PIVA_A)
        _invoice(
            test_db_session, "993/2026", customer_id=bad.id,
            customer_name_raw="QOQA di Amanda", customer_piva_raw=PIVA_A,
        )
        clean = _customer(test_db_session, "Rossi SRL", partita_iva=PIVA_A)
        _invoice(
            test_db_session, "R1/2026", customer_id=clean.id,
            customer_name_raw="Rossi SRL", customer_piva_raw=PIVA_A,
        )
        data = test_client.get("/api/customers?to_sanitize=true&only_overdue=false").json()
        ids = [i["id"] for i in data["items"]]
        assert bad.id in ids
        assert clean.id not in ids

    def test_no_phone_filter(self, test_client, test_db_session):
        with_phone = _customer(test_db_session, "Con Tel", phone="+39 333 111 2222")
        no_phone = _customer(test_db_session, "Senza Tel", phone=None)
        empty_phone = _customer(test_db_session, "Vuoto Tel", phone="   ")
        data = test_client.get("/api/customers?no_phone=true&only_overdue=false").json()
        ids = [i["id"] for i in data["items"]]
        assert no_phone.id in ids
        assert empty_phone.id in ids
        assert with_phone.id not in ids

    def test_recovery_status_filter(self, test_client, test_db_session):
        lawyer = _customer(test_db_session, "Da Avvocato", recovery_status="lawyer")
        idle = _customer(test_db_session, "Tranquillo", recovery_status="idle")
        data = test_client.get(
            "/api/customers?recovery_status=lawyer&only_overdue=false"
        ).json()
        ids = [i["id"] for i in data["items"]]
        assert lawyer.id in ids
        assert idle.id not in ids

    def test_sort_by_days_overdue(self, test_client, test_db_session):
        c1 = _customer(test_db_session, "Poco Scaduto", partita_iva=PIVA_A)
        _invoice(test_db_session, "A/2026", customer_id=c1.id, days_overdue=5)
        c2 = _customer(test_db_session, "Molto Scaduto", partita_iva=PIVA_B)
        _invoice(test_db_session, "B/2026", customer_id=c2.id, days_overdue=90)
        data = test_client.get(
            "/api/customers?sort_by=days_overdue&sort_order=desc"
        ).json()
        ids = [i["id"] for i in data["items"]]
        assert ids.index(c2.id) < ids.index(c1.id)
        # e il campo è esposto
        top = next(i for i in data["items"] if i["id"] == c2.id)
        assert top["max_days_overdue"] == 90

    def test_sort_by_last_action(self, test_client, test_db_session):
        recent = _customer(test_db_session, "Sollecitato Ieri", partita_iva=PIVA_A)
        _invoice(test_db_session, "A/2026", customer_id=recent.id, days_overdue=10)
        old = _customer(test_db_session, "Sollecitato Mesi Fa", partita_iva=PIVA_B)
        _invoice(test_db_session, "B/2026", customer_id=old.id, days_overdue=10)
        test_db_session.add(RecoveryAction(
            customer_id=recent.id, action_type="first_contact",
            created_at=datetime.utcnow() - timedelta(days=1),
        ))
        test_db_session.add(RecoveryAction(
            customer_id=old.id, action_type="first_contact",
            created_at=datetime.utcnow() - timedelta(days=90),
        ))
        test_db_session.commit()
        data = test_client.get(
            "/api/customers?sort_by=last_action&sort_order=desc"
        ).json()
        ids = [i["id"] for i in data["items"]]
        assert ids.index(recent.id) < ids.index(old.id)
        top = next(i for i in data["items"] if i["id"] == recent.id)
        assert top["last_action"] is not None

    def test_cancelled_action_not_counted_as_last_action(self, test_client, test_db_session):
        c = _customer(test_db_session, "Solo Annullato", partita_iva=PIVA_A)
        _invoice(test_db_session, "A/2026", customer_id=c.id, days_overdue=10)
        test_db_session.add(RecoveryAction(
            customer_id=c.id, action_type="first_contact",
            cancelled=True, created_at=datetime.utcnow(),
        ))
        test_db_session.commit()
        data = test_client.get("/api/customers?only_overdue=false").json()
        row = next(i for i in data["items"] if i["id"] == c.id)
        assert row["last_action"] is None
