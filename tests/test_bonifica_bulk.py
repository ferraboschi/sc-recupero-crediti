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


# ── B) GET /customers/bonifica-suggestions ───────────────────────────

class TestBonificaSuggestionsList:
    def test_includes_only_valid_candidates(self, test_client, test_db_session):
        # Candidato: senza P.IVA, fatture con UNA P.IVA valida concorde.
        cand = _customer(test_db_session, CAVO)
        _invoice(test_db_session, "C1/2026", customer_id=cand.id,
                 customer_name_raw=CAVO, customer_piva_raw=PIVA_CAVO, amount_due=500.0)
        # Escluso: ha già una P.IVA valida.
        has_piva = _customer(test_db_session, "Rossi SRL", partita_iva=PIVA_A)
        _invoice(test_db_session, "H/2026", customer_id=has_piva.id,
                 customer_name_raw="Rossi SRL", customer_piva_raw=PIVA_A)
        # Escluso: fatture con P.IVA DIVERSE (conflitto → forse due clienti).
        conflict = _customer(test_db_session, "Doppio SRL")
        _invoice(test_db_session, "D1/2026", customer_id=conflict.id,
                 customer_name_raw="Doppio SRL", customer_piva_raw=PIVA_A)
        _invoice(test_db_session, "D2/2026", customer_id=conflict.id,
                 customer_name_raw="Doppio SRL", customer_piva_raw=PIVA_B)
        # Escluso: nessuna P.IVA valida da assegnare (caso Tier 2).
        no_piva = _customer(test_db_session, "Ferramenta Bianchi")
        _invoice(test_db_session, "NP/2026", customer_id=no_piva.id,
                 customer_name_raw="Sushi Kyoto")

        data = test_client.get("/api/customers/bonifica-suggestions").json()
        ids = [i["customer_id"] for i in data["items"]]
        assert ids == [cand.id]
        assert data["total"] == 1
        item = data["items"][0]
        assert item["ragione_sociale"] == CAVO
        assert item["piva_suggerita"] == PIVA_CAVO
        assert item["invoice_count"] == 1
        assert item["confidence"] == 100
        assert item["total_overdue"] == 500.0

    def test_paid_invoice_piva_not_counted(self, test_client, test_db_session):
        # Una fattura PAGATA non deve rendere bonificabile un cliente (la lista
        # guarda le non-pagate, come lo scaduto).
        c = _customer(test_db_session, CAVO)
        _invoice(test_db_session, "P/2026", customer_id=c.id, status="paid",
                 customer_name_raw=CAVO, customer_piva_raw=PIVA_CAVO)
        data = test_client.get("/api/customers/bonifica-suggestions").json()
        assert c.id not in [i["customer_id"] for i in data["items"]]

    def test_reviewed_invoice_excluded_like_audit(self, test_client, test_db_session):
        # Coerenza con bonifica_piva: una fattura "Segnata verificata" non porta
        # più il suo P.IVA nella bonifica.
        c = _customer(test_db_session, CAVO)
        inv = _invoice(test_db_session, "R/2026", customer_id=c.id,
                       customer_name_raw=CAVO, customer_piva_raw=PIVA_CAVO)
        assert c.id in [i["customer_id"] for i in
                        test_client.get("/api/customers/bonifica-suggestions").json()["items"]]
        test_client.post(f"/api/positions/{inv.id}/mark-reviewed")
        assert c.id not in [i["customer_id"] for i in
                            test_client.get("/api/customers/bonifica-suggestions").json()["items"]]

    def test_ordered_by_confidence_then_overdue(self, test_client, test_db_session):
        # Certezza alta prima; a parità di certezza, scaduto più alto prima.
        high = _customer(test_db_session, CAVO)  # nome identico → 100
        _invoice(test_db_session, "HI/2026", customer_id=high.id,
                 customer_name_raw=CAVO, customer_piva_raw=PIVA_CAVO, amount_due=100.0)
        low = _customer(test_db_session, "Trattoria Del Sole SRL")
        _invoice(test_db_session, "LO/2026", customer_id=low.id,
                 customer_name_raw="Officina Meccanica Verdi", customer_piva_raw=PIVA_A,
                 amount_due=9999.0)  # scaduto enorme ma confidence bassa
        data = test_client.get("/api/customers/bonifica-suggestions").json()
        ids = [i["customer_id"] for i in data["items"]]
        # confidence vince sul totale scaduto.
        assert ids.index(high.id) < ids.index(low.id)

    def test_tiebreak_by_overdue_when_same_confidence(self, test_client, test_db_session):
        # Stessa confidence (nome identico → 100): vince lo scaduto più alto.
        big = _customer(test_db_session, "Alpha Beverage SRL")
        _invoice(test_db_session, "BIG/2026", customer_id=big.id,
                 customer_name_raw="Alpha Beverage SRL", customer_piva_raw=PIVA_A,
                 amount_due=8000.0)
        small = _customer(test_db_session, "Beta Beverage SRL")
        _invoice(test_db_session, "SML/2026", customer_id=small.id,
                 customer_name_raw="Beta Beverage SRL", customer_piva_raw=PIVA_B,
                 amount_due=10.0)
        data = test_client.get("/api/customers/bonifica-suggestions").json()
        ids = [i["customer_id"] for i in data["items"]]
        assert ids.index(big.id) < ids.index(small.id)


# ── C) efficienza: conteggio query costante ──────────────────────────

class TestBonificaSuggestionsEfficiency:
    def test_query_count_constant_vs_customer_count(self, test_client, test_db_session):
        # La lista NON deve fare una query per cliente (niente N+1 su 115+):
        # il numero di SELECT è COSTANTE al crescere dei clienti.
        def make(start, n):
            for i in range(start, start + n):
                c = _customer(test_db_session, f"Cliente {i} Beverage SRL")
                _invoice(test_db_session, f"I{i}/2026", customer_id=c.id,
                         customer_name_raw=f"Cliente {i} Beverage SRL",
                         customer_piva_raw=PIVA_CAVO)

        make(0, 3)
        n_small = _count_selects(
            test_db_session,
            lambda: test_client.get("/api/customers/bonifica-suggestions"),
        )
        make(1000, 30)  # 10× i clienti candidati
        n_big = _count_selects(
            test_db_session,
            lambda: test_client.get("/api/customers/bonifica-suggestions"),
        )
        assert n_small == n_big, (
            f"query cresciute col n. clienti: {n_small} → {n_big} (N+1)"
        )
        assert n_small <= 2, f"attesa UNA passata aggregata, non {n_small} SELECT"


# ── D) POST /customers/bonifica-piva/bulk ────────────────────────────

def _bulk(test_client, ids):
    return test_client.post(
        "/api/customers/bonifica-piva/bulk", json={"customer_ids": ids}
    ).json()


class TestBonificaBulkApply:
    def test_applies_and_cascades(self, test_client, test_db_session):
        c = _customer(test_db_session, CAVO)
        _invoice(test_db_session, "C1/2026", customer_id=c.id,
                 customer_name_raw=CAVO, customer_piva_raw=PIVA_CAVO)
        _invoice(test_db_session, "C2/2026", customer_id=c.id,
                 customer_name_raw=CAVO, customer_piva_raw=PIVA_CAVO)
        out = _bulk(test_client, [c.id])
        assert out["applied"] == 1
        assert out["results"] == [{"customer_id": c.id, "result": "applied", "piva": PIVA_CAVO}]
        # La P.IVA è sul cliente e la CASCADE rende verdi tutte le fatture.
        detail = test_client.get(f"/api/customers/{c.id}").json()
        assert detail["partita_iva"] == PIVA_CAVO
        for it in detail["invoices"]["items"]:
            assert it["verification"]["level"] == "verified"
        # Loggato in ActivityLog.
        assert test_db_session.query(ActivityLog).filter_by(
            action="audit_assign_piva", entity_id=c.id
        ).count() == 1

    def test_skips_has_piva_and_is_idempotent(self, test_client, test_db_session):
        # Già valorizzata → skipped_has_piva; e ri-eseguire un bonificato NON
        # è un errore (idempotenza), è di nuovo skipped_has_piva.
        c = _customer(test_db_session, CAVO)
        _invoice(test_db_session, "C1/2026", customer_id=c.id,
                 customer_name_raw=CAVO, customer_piva_raw=PIVA_CAVO)
        first = _bulk(test_client, [c.id])
        assert first["results"][0]["result"] == "applied"
        second = _bulk(test_client, [c.id])
        assert second["applied"] == 0
        assert second["results"][0]["result"] == "skipped_has_piva"

    def test_revalidates_conflict_server_side(self, test_client, test_db_session):
        # Il client crede sia bonificabile ma le fatture portano P.IVA DIVERSE:
        # il server RI-VALIDA e salta (non si fida del client).
        c = _customer(test_db_session, "Doppio SRL")
        _invoice(test_db_session, "D1/2026", customer_id=c.id,
                 customer_name_raw="Doppio SRL", customer_piva_raw=PIVA_A)
        _invoice(test_db_session, "D2/2026", customer_id=c.id,
                 customer_name_raw="Doppio SRL", customer_piva_raw=PIVA_B)
        out = _bulk(test_client, [c.id])
        assert out["applied"] == 0
        assert out["results"][0]["result"] == "skipped_conflict"
        assert out["results"][0]["pivas"] == sorted([PIVA_A, PIVA_B])
        # Nessuna P.IVA scritta.
        assert test_db_session.query(Customer).filter_by(id=c.id).first().partita_iva is None

    def test_not_found_for_unknown_id(self, test_client, test_db_session):
        out = _bulk(test_client, [999999])
        assert out["results"][0]["result"] == "not_found"

    def test_no_piva_when_nothing_to_assign(self, test_client, test_db_session):
        # Cliente senza P.IVA sulle fatture (caso Tier 2): niente da assegnare.
        c = _customer(test_db_session, "Ferramenta Bianchi")
        _invoice(test_db_session, "NP/2026", customer_id=c.id, customer_name_raw="Sushi Kyoto")
        out = _bulk(test_client, [c.id])
        assert out["results"][0]["result"] == "skipped_no_piva"

    def test_mixed_batch_partitions_outcomes(self, test_client, test_db_session):
        ok = _customer(test_db_session, CAVO)
        _invoice(test_db_session, "OK/2026", customer_id=ok.id,
                 customer_name_raw=CAVO, customer_piva_raw=PIVA_CAVO)
        has = _customer(test_db_session, "Rossi SRL", partita_iva=PIVA_A)
        _invoice(test_db_session, "H/2026", customer_id=has.id,
                 customer_name_raw="Rossi SRL", customer_piva_raw=PIVA_A)
        out = _bulk(test_client, [ok.id, has.id, 424242])
        by_id = {r["customer_id"]: r["result"] for r in out["results"]}
        assert out["applied"] == 1
        assert by_id[ok.id] == "applied"
        assert by_id[has.id] == "skipped_has_piva"
        assert by_id[424242] == "not_found"
