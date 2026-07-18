"""BONIFICA P.IVA IN BLOCCO — lista di revisione + apply massivo.

L'owner ha ~115 clienti senza P.IVA sul profilo ma con UNA sola P.IVA valida
e concorde sulle loro fatture (da FatturaPro): qui si bonificano in blocco
invece che uno a uno. Copertura:

- A) confidence su bonifica_piva: somiglianza nome (stesso scorer di verify),
     MINIMO sul gruppo (caso peggiore).
- B) GET /customers/bonifica-suggestions: solo i candidati giusti, esclude i
     conflitto-P.IVA e i già-con-P.IVA, ordina per confidence poi scaduto.
- C) efficienza: conteggio query COSTANTE rispetto al numero di clienti.
- D) POST /customers/bonifica-piva/bulk: applica + salta i casi limite +
     RI-VALIDA server-side + idempotente.
- E) POST /customers/{id}/clear-piva: reversibilità della bonifica.
- F) un cliente bonificato ESCE dalla lista al giro dopo.
"""

from sqlalchemy import event

from backend.database import Customer, Invoice, ActivityLog
from backend.engine.normalizer import name_similarity_score

# P.IVA italiane checksum-valide (vedi backend/engine/piva.py)
PIVA_A = "12345678903"
PIVA_B = "98765432103"
PIVA_CAVO = "02572440994"

# Caso reale dell'owner: cliente senza P.IVA, fatture con la stessa P.IVA
# valida e nome identico → il giallo nasce SOLO dalla P.IVA mancante.
CAVO = "Cavo Luigi Beverage Solutions srl"


def _customer(session, name, **kw):
    c = Customer(ragione_sociale=name, source=kw.pop("source", "shopify"), **kw)
    session.add(c)
    session.commit()
    return c


def _invoice(session, number, customer_id=None, **kw):
    from datetime import date
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


def _count_selects(test_db_session, do_request):
    """Numero di SELECT emessi dall'engine durante do_request()."""
    statements = []
    engine = test_db_session.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        do_request()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    return len(statements)


# ── A) confidence su bonifica_piva ───────────────────────────────────

class TestBonificaPivaConfidence:
    def test_confidence_100_when_name_identical(self, test_client, test_db_session):
        # Ragione sociale identica all'intestazione → certezza ≈ 100.
        c = _customer(test_db_session, CAVO)  # nessuna P.IVA
        _invoice(test_db_session, "C1/2026", customer_id=c.id,
                 customer_name_raw=CAVO, customer_piva_raw=PIVA_CAVO)
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert data["bonifica_piva"]["confidence"] == 100

    def test_confidence_uses_verify_scorer(self, test_client, test_db_session):
        # La confidence usa LO STESSO scorer di verify ("Somiglianza nomi: X%").
        c = _customer(test_db_session, CAVO)
        _invoice(test_db_session, "C1/2026", customer_id=c.id,
                 customer_name_raw="Cavo L. Beverage", customer_piva_raw=PIVA_CAVO)
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        expected = name_similarity_score("Cavo L. Beverage", CAVO)
        assert data["bonifica_piva"]["confidence"] == expected
        # E coincide col name_score del pannello per-riga della stessa fattura.
        assert data["items"][0]["name_score"] == expected

    def test_confidence_is_minimum_over_group(self, test_client, test_db_session):
        # Due fatture STESSA P.IVA, intestazioni diverse → confidence = MIN
        # (caso peggiore, conservativo).
        c = _customer(test_db_session, CAVO)
        _invoice(test_db_session, "C1/2026", customer_id=c.id,
                 customer_name_raw=CAVO, customer_piva_raw=PIVA_CAVO)  # 100
        _invoice(test_db_session, "C2/2026", customer_id=c.id,
                 customer_name_raw="Bar Sport Milano", customer_piva_raw=PIVA_CAVO)
        low = name_similarity_score("Bar Sport Milano", CAVO)
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert low < 100
        assert data["bonifica_piva"]["invoice_count"] == 2
        assert data["bonifica_piva"]["confidence"] == low  # il minimo, non 100
