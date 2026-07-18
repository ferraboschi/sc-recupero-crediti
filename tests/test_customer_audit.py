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

    def test_counts_exclude_reviewed_problems(self, test_client, test_db_session):
        # DIFETTO 3: un cliente col suo UNICO problema già "Segnato verificato"
        # non deve dichiarare total_problems=1 con zero item azionabili: i
        # contatori counts/total_problems contano i problemi AZIONABILI
        # (coerenti con items/problem_count/worst_verdict — è ciò che il
        # frontend affianca: tile rossa vs badge "In ordine ✓"). I reviewed
        # restano tracciati da reviewed_count ("già verificate").
        c = _customer(test_db_session, "Rooftop SRL", partita_iva=PIVA_A)
        inv = _invoice(
            test_db_session, "993/2026", customer_id=c.id,
            customer_name_raw="QOQA di Amanda Piccolo", customer_piva_raw=PIVA_A,
        )
        assert test_client.post(f"/api/positions/{inv.id}/mark-reviewed").status_code == 200
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert data["counts"]["bad"] == 0
        assert data["counts"]["warn"] == 0
        assert data["total_problems"] == 0
        assert data["problem_count"] == 0
        assert data["worst_verdict"] == "ok"
        assert data["reviewed_count"] == 1
        # Con include_reviewed=true la fattura rientra fra gli item E nei
        # contatori: counts descrive sempre ciò che items mostra.
        data = test_client.get(
            f"/api/customers/{c.id}/audit?include_reviewed=true"
        ).json()
        assert data["counts"]["bad"] == 1
        assert data["total_problems"] == 1
        assert data["problem_count"] == 1
        assert data["worst_verdict"] == "bad"

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

    def test_pending_on_paid_invoice_counted_everywhere(self, test_client, test_db_session):
        # DIFETTO 1: cliente il cui UNICO segnale è un suggerimento pendente su
        # fattura PAGATA. La scheda (/audit) lo segnala "da abbinare": allora
        # DEVE comparire in TUTTI E TRE gli insiemi — audit-summary, filtro
        # to_sanitize della lista, e /audit con worst_verdict != ok.
        # (Precedente Belfiore, docs/verifica-segnalazioni-20260716.md: le
        # quarantenate pagate restano visibili sul profilo perché inquinano i
        # totali storici finché non vengono abbinate.)
        c = _customer(test_db_session, "Belfiore M & M srl", partita_iva=PIVA_A)
        _invoice(
            test_db_session, "655/2026", customer_id=None, status="paid",
            suggested_customer_id=c.id, suggested_method="fuzzy",
            suggested_score=70, customer_name_raw="Belfiore M&M",
        )
        # 1) audit-summary
        data = test_client.get("/api/customers/audit-summary").json()
        assert c.id in data["customer_ids"]
        assert data["to_sanitize_count"] == 1
        # 2) lista con filtro to_sanitize
        data = test_client.get("/api/customers?to_sanitize=true&only_overdue=false").json()
        assert c.id in [i["id"] for i in data["items"]]
        # 3) audit del cliente: il pendente pagato è visibile e pesa sul verdetto
        data = test_client.get(f"/api/customers/{c.id}/audit").json()
        assert data["pending_count"] == 1
        assert data["worst_verdict"] != "ok"

    def test_orphan_suggested_customer_not_counted(self, test_client, test_db_session):
        # DIFETTO 2: un suggested_customer_id che punta a un cliente CANCELLATO
        # (merge/cleanup) non deve gonfiare il badge "da sanificare" con un id
        # fantasma che la lista poi non sa mostrare.
        _invoice(
            test_db_session, "GHOST/2026", customer_id=None,
            suggested_customer_id=99999, suggested_method="fuzzy",
            suggested_score=70, customer_name_raw="Cliente Sparito",
        )
        data = test_client.get("/api/customers/audit-summary").json()
        assert data["to_sanitize_count"] == 0
        assert data["customer_ids"] == []

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


# ── DIFETTO 4: ordinamenti deterministici (paginazione stabile) ──────
#
# LIMITE DOCUMENTATO: su SQLite l'instabilità di produzione NON è
# riproducibile — senza ORDER BY, SQLite ritorna comunque l'ordine rowid
# (deterministico), mentre Postgres/Supabase no: i pari-merito si spostano
# fra richieste e la paginazione salta/ripete righe. Perciò qui si verifica
# su due binari: (a) che l'ORDER BY esista DAVVERO nell'SQL emesso
# (catturando gli statement — questo è rosso senza il fix), e (b) il
# contratto comportamentale: pari-merito sempre in id crescente, e due
# chiamate identiche danno lo stesso ordine.

def _capture_sql(test_db_session, do_request):
    """Esegue do_request() catturando gli statement SQL emessi dall'engine."""
    from sqlalchemy import event

    statements = []
    engine = test_db_session.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        do_request()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    return statements


class TestStableOrdering:
    def _three_tied_customers(self, session):
        """Tre clienti con lo STESSO totale scaduto (pari-merito garantito)."""
        out = []
        for name in ("Pari Uno", "Pari Due", "Pari Tre"):
            c = _customer(session, name)
            _invoice(
                session, f"{name}/2026", customer_id=c.id,
                amount_due=100.0, days_overdue=10,
            )
            out.append(c)
        return out

    def test_list_base_query_has_order_by(self, test_client, test_db_session):
        # La SELECT base sui clienti deve avere ORDER BY customers.id: senza,
        # su Postgres l'ordine di partenza del sort Python non è garantito.
        self._three_tied_customers(test_db_session)
        statements = _capture_sql(
            test_db_session,
            lambda: test_client.get(
                "/api/customers?sort_by=total_overdue&sort_order=desc&only_overdue=false"
            ),
        )
        base_selects = [
            s for s in statements
            if s.lstrip().upper().startswith("SELECT")
            and "FROM customers" in s and "JOIN" not in s and "GROUP BY" not in s
        ]
        assert base_selects, "attesa la SELECT base sui clienti"
        assert any(
            "ORDER BY" in s and "customers.id" in s.split("ORDER BY", 1)[1]
            for s in base_selects
        ), f"nessun ORDER BY customers.id nella query base: {base_selects}"

    def test_sort_ties_deterministic_by_id(self, test_client, test_db_session):
        a, b, c = self._three_tied_customers(test_db_session)
        url = "/api/customers?sort_by=total_overdue&sort_order=desc&only_overdue=false"
        ids1 = [i["id"] for i in test_client.get(url).json()["items"]]
        ids2 = [i["id"] for i in test_client.get(url).json()["items"]]
        # Ripetibilità: due chiamate identiche, stesso ordine.
        assert ids1 == ids2
        # Pari-merito in id CRESCENTE anche in desc (tiebreaker esplicito),
        # coerente con /neighbors (ORDER BY … DESC, id ASC).
        tied = [i for i in ids1 if i in {a.id, b.id, c.id}]
        assert tied == sorted([a.id, b.id, c.id])

    def test_sort_ties_deterministic_by_id_asc(self, test_client, test_db_session):
        a, b, c = self._three_tied_customers(test_db_session)
        url = "/api/customers?sort_by=total_overdue&sort_order=asc&only_overdue=false"
        ids = [i["id"] for i in test_client.get(url).json()["items"]]
        tied = [i for i in ids if i in {a.id, b.id, c.id}]
        assert tied == sorted([a.id, b.id, c.id])

    def test_neighbors_order_by_has_id_tiebreaker(self, test_client, test_db_session):
        # /neighbors ordina per total_overdue desc: senza tiebreaker id i
        # pari-merito rendono prev/next non deterministici su Postgres.
        a, b, c = self._three_tied_customers(test_db_session)
        statements = _capture_sql(
            test_db_session,
            lambda: test_client.get(f"/api/customers/{b.id}/neighbors"),
        )
        neighbor_selects = [
            s for s in statements
            if "FROM customers JOIN invoices" in s and "ORDER BY" in s
        ]
        assert neighbor_selects, "attesa la query di /neighbors"
        assert any(
            "customers.id" in s.split("ORDER BY", 1)[1] for s in neighbor_selects
        ), f"nessun tiebreaker customers.id nell'ORDER BY: {neighbor_selects}"

    def test_neighbors_ties_follow_id_order(self, test_client, test_db_session):
        a, b, c = self._three_tied_customers(test_db_session)
        data = test_client.get(f"/api/customers/{b.id}/neighbors").json()
        assert data["prev_id"] == a.id
        assert data["next_id"] == c.id
        assert data["position"] == 2
        assert data["total"] == 3
