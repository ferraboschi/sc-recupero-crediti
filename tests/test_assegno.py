"""Assegno in mano (Fase 3, decisioni owner).

Q1 ibrido: la fattura pagata con assegno da incassare ESCE dal lavorabile ma
RESTA nell'universo (bucket 'in_incasso'); Q2: conta come recuperato dalla
registrazione, in sotto-voce separata dalla cassa; Q3: l'insoluto riporta
SUBITO la fattura scaduta, riapre la STESSA pratica (storico intatto), storna
il recuperato (derivato) e accende l'allarme. amount_due MAI azzerato.
"""
from datetime import date, datetime, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryAction, RecoveryCase
from backend.engine.cases import ensure_open_case, attach_overdue_invoices, get_open_case
from backend.engine.action_invoices import set_action_invoices, per_invoice_sollecito_stats
from backend.engine.overdue import compute_overdue_buckets, is_overdue_unpaid


@pytest.fixture
def okasan(test_db_session):
    cust = Customer(ragione_sociale="Okasan S.R.L.")
    test_db_session.add(cust)
    test_db_session.commit()
    today = date.today()
    a = Invoice(invoice_number="FT-A", amount=1000.0, amount_due=1000.0,
                issue_date=today - timedelta(days=70), due_date=today - timedelta(days=40),
                days_overdue=40, status="open", customer_id=cust.id, source_platform="fatturapro")
    b = Invoice(invoice_number="FT-B", amount=500.0, amount_due=500.0,
                issue_date=today - timedelta(days=50), due_date=today - timedelta(days=20),
                days_overdue=20, status="open", customer_id=cust.id, source_platform="fatturapro")
    test_db_session.add_all([a, b])
    test_db_session.commit()
    case = ensure_open_case(test_db_session, cust)
    attach_overdue_invoices(test_db_session, cust, case)
    when = datetime.utcnow() - timedelta(days=10)
    act = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="first_contact",
                         completed_at=when, created_at=when, outcome="contacted",
                         channel="whatsapp_copy", invoice_ids=[a.id, b.id])
    test_db_session.add(act)
    test_db_session.flush()
    set_action_invoices(test_db_session, act.id, [a.id, b.id])
    test_db_session.commit()
    return cust, a, b, case


FUTURE = (date.today() + timedelta(days=30)).isoformat()


def test_register_assegno_moves_bucket_not_amount(test_client, test_db_session, okasan):
    cust, a, b, case = okasan
    r = test_client.post(f"/api/positions/{a.id}/assegno",
                         json={"expected_date": FUTURE, "note": "assegno n.123, incasso 30gg"})
    assert r.status_code == 200, r.text
    assert r.json()["in_incasso"] is True
    test_db_session.refresh(a)
    assert a.amount_due == 1000.0 and a.status == "open"     # MAI azzerato, mai 'paid'
    assert a.payment_pending == "assegno" and a.payment_pending_amount == 1000.0
    assert not is_overdue_unpaid(a)
    bk = compute_overdue_buckets(test_db_session)
    assert bk["in_incasso"] == {"fatture": 1, "importo": 1000.0}
    assert bk["lavorabile"] == {"fatture": 1, "importo": 500.0}
    assert bk["scaduto_totale"]["importo"] == 1500.0          # identità universo = Σ bucket


def test_riconciliazione_recuperato_sottovoce(test_client, okasan):
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={"expected_date": FUTURE})
    rec = test_client.get("/api/dashboard/riconciliazione").json()
    assert rec["cascata"]["in_incasso"]["importo"] == 1000.0
    assert rec["cascata"]["lavorabile"]["importo"] == 500.0
    assert rec["recuperato"]["in_incasso_assegni"]["importo"] == 1000.0
    assert rec["recuperato"]["certo"]["importo"] == 0.0        # cassa vera: nessuna
    assert rec["recuperato"]["totale"]["importo"] == 1000.0
    assert "in_incasso" in rec["precedenza"]


def test_customer_detail_exposes_state(test_client, okasan):
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={"expected_date": FUTURE, "note": "n.123"})
    det = test_client.get(f"/api/customers/{cust.id}").json()
    by = {i["id"]: i for i in det["invoices"]["items"]}
    assert by[a.id]["in_incasso"] is True and by[a.id]["payment_pending"] == "assegno"
    assert by[a.id]["payment_pending_note"] == "n.123"
    assert by[b.id]["in_incasso"] is False


def test_insoluto_reopens_same_case_and_storna(test_client, test_db_session, okasan):
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={"expected_date": FUTURE})
    test_client.post(f"/api/positions/{b.id}/assegno", json={"expected_date": FUTURE})
    # nessuna scaduta lavorabile → la pratica si chiude (no_overdue)
    test_db_session.refresh(case)
    assert case.status == "closed" and case.closed_reason == "in_incasso"
    assert get_open_case(test_db_session, cust.id) is None
    # INSOLUTO su A → torna scaduta SUBITO, la STESSA pratica si riapre
    r = test_client.post(f"/api/positions/{a.id}/assegno/insoluto", json={"note": "tornato indietro"})
    assert r.status_code == 200, r.text
    assert r.json()["in_incasso"] is False and r.json()["bounced_at"] is not None
    reopened = get_open_case(test_db_session, cust.id)
    assert reopened is not None and reopened.id == case.id
    test_db_session.refresh(a)
    assert is_overdue_unpaid(a) and a.amount_due == 1000.0
    # storico solleciti intatto (per-fattura)
    assert per_invoice_sollecito_stats(test_db_session, [a.id])[a.id]["count"] == 1
    bk = compute_overdue_buckets(test_db_session)
    assert bk["lavorabile"]["importo"] == 1000.0 and bk["in_incasso"]["importo"] == 500.0
    rec = test_client.get("/api/dashboard/riconciliazione").json()
    assert rec["recuperato"]["in_incasso_assegni"]["importo"] == 500.0   # storno di A
    dash = test_client.get("/api/dashboard").json()
    assert dash["assegni"]["insoluti"]["fatture"] == 1
    assert dash["assegni"]["insoluti"]["items"][0]["invoice_id"] == a.id
    assert dash["assegni"]["in_incasso"]["fatture"] == 1


def test_new_assegno_after_bounce_clears_alarm(test_client, test_db_session, okasan):
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    test_client.post(f"/api/positions/{a.id}/assegno/insoluto")
    r = test_client.post(f"/api/positions/{a.id}/assegno", json={"note": "nuovo assegno"})
    assert r.status_code == 200 and r.json()["in_incasso"] is True and r.json()["bounced_at"] is None


def test_cancel_assegno(test_client, test_db_session, okasan):
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    r = test_client.delete(f"/api/positions/{a.id}/assegno")
    assert r.status_code == 200 and r.json()["payment_pending"] is None
    test_db_session.refresh(a)
    assert is_overdue_unpaid(a)
    # dopo un insoluto NON si annulla: si registra un nuovo assegno
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    test_client.post(f"/api/positions/{a.id}/assegno/insoluto")
    assert test_client.delete(f"/api/positions/{a.id}/assegno").status_code == 409


def test_guards(test_client, test_db_session, okasan):
    cust, a, b, case = okasan
    assert test_client.post(f"/api/positions/{a.id}/assegno/insoluto").status_code == 409
    assert test_client.post(f"/api/positions/{a.id}/assegno", json={"expected_date": "30/11/2026"}).status_code == 400
    a.status = "paid"
    test_db_session.commit()
    assert test_client.post(f"/api/positions/{a.id}/assegno", json={}).status_code == 409


def test_avvocato_debt_excludes_in_incasso(test_client, test_db_session, okasan):
    from backend.api.avvocato import _overdue_invoices
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    ids = {i.id for i in _overdue_invoices(test_db_session, cust.id)}
    assert ids == {b.id}


def test_expected_date_passed_flag(test_client, okasan):
    cust, a, b, case = okasan
    past = (date.today() - timedelta(days=3)).isoformat()
    test_client.post(f"/api/positions/{a.id}/assegno", json={"expected_date": past})
    det = test_client.get(f"/api/customers/{cust.id}").json()
    inv = next(i for i in det["invoices"]["items"] if i["id"] == a.id)
    assert inv["pending_overdue"] is True
    assert test_client.get("/api/dashboard").json()["assegni"]["oltre_data_prevista"] == 1


# ── Regressioni dalla review avversariale ───────────────────────────

def test_history_backfill_identity_with_in_incasso(test_client, test_db_session, okasan):
    """B1: le righe STIMATE dello storico devono chiudere anche col bucket nuovo."""
    from backend.engine.overdue_history import backfill_overdue_history
    from backend.database import OverdueSnapshot
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    backfill_overdue_history(test_db_session, days=3)
    rows = test_db_session.query(OverdueSnapshot).all()
    assert rows
    for r in rows:
        assert round(r.non_abbinati + r.esclusi + r.contestati + r.in_incasso + r.lavorabile, 2) == round(r.scaduto_totale, 2)


def test_insoluto_never_reopens_archived_case(test_client, test_db_session, okasan):
    """M1: un'archiviazione non si scavalca: l'insoluto NON riapre la pratica
    archiviata; il lifecycle ne apre una nuova (che eredita i contatti)."""
    from backend.engine.cases import close_case
    from backend.database import ActivityLog
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    test_client.post(f"/api/positions/{b.id}/assegno", json={})
    test_db_session.refresh(case)
    # archivio la pratica chiusa: simulo la decisione dell'operatore
    case.status = "open"; test_db_session.commit()
    close_case(test_db_session, case, "archived"); test_db_session.commit()
    r = test_client.post(f"/api/positions/{a.id}/assegno/insoluto")
    assert r.status_code == 200 and r.json()["bounced_at"] is not None
    test_db_session.refresh(case)
    # l'archiviazione resta (regola del motore: il debito archiviato non riapre
    # nulla); l'ALLARME però c'è: riga + cruscotto. L'operatore decide se
    # riaprire togliendo l'archiviazione.
    assert case.status == "closed" and case.closed_reason == "archived"
    assert get_open_case(test_db_session, cust.id) is None
    dash = test_client.get("/api/dashboard").json()
    assert dash["assegni"]["insoluti"]["fatture"] == 1


def test_insoluto_on_disputed_does_not_reopen(test_client, test_db_session, okasan):
    """M2: se la fattura è stata contestata dopo l'assegno, l'insoluto non
    riapre nulla (la fattura non è tornata lavorabile)."""
    from backend.database import ActivityLog
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    test_client.post(f"/api/positions/{b.id}/assegno", json={})
    test_client.put(f"/api/positions/{a.id}/status", params={"new_status": "disputed"})
    n_before = test_db_session.query(ActivityLog).filter_by(action="case_reopened").count()
    r = test_client.post(f"/api/positions/{a.id}/assegno/insoluto")
    assert r.status_code == 200
    assert test_db_session.query(ActivityLog).filter_by(action="case_reopened").count() == n_before
    assert get_open_case(test_db_session, cust.id) is None


def test_insoluto_reopen_is_explicit_not_fallback(test_client, test_db_session, okasan):
    """M3: la riapertura passa dalla STESSA pratica, non da un'apertura nuova
    che fallisce sull'indice unico."""
    from backend.database import ActivityLog
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    test_client.post(f"/api/positions/{b.id}/assegno", json={})
    n_opened = test_db_session.query(ActivityLog).filter_by(action="case_opened").count()
    test_client.post(f"/api/positions/{a.id}/assegno/insoluto")
    assert test_db_session.query(ActivityLog).filter_by(action="case_opened").count() == n_opened
    assert test_db_session.query(ActivityLog).filter_by(action="case_reopened", entity_id=case.id).count() == 1


def test_partial_payment_keeps_three_figures_consistent(test_client, test_db_session, okasan):
    """M4: cascata, cruscotto e sotto-voce usano lo stesso residuo VIVO."""
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    a.amount_due = 600.0  # acconto arrivato via sync
    test_db_session.commit()
    bk = compute_overdue_buckets(test_db_session)
    rec = test_client.get("/api/dashboard/riconciliazione").json()
    dash = test_client.get("/api/dashboard").json()
    assert bk["in_incasso"]["importo"] == 600.0
    assert rec["recuperato"]["in_incasso_assegni"]["importo"] == 600.0
    assert dash["assegni"]["in_incasso"]["importo"] == 600.0


def test_paid_by_sync_leaves_in_incasso_everywhere(test_client, test_db_session, okasan):
    """m5: pagata su FatturaPro mentre era in incasso: esce da bucket,
    sotto-voce e cruscotto; il certo la contabilizza una volta sola."""
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    a.status = "paid"; a.paid_at = datetime.utcnow(); a.amount_due_at_paid = a.amount_due
    test_db_session.commit()
    assert compute_overdue_buckets(test_db_session)["in_incasso"]["fatture"] == 0
    rec = test_client.get("/api/dashboard/riconciliazione").json()
    assert rec["recuperato"]["in_incasso_assegni"]["importo"] == 0.0
    assert rec["recuperato"]["certo"]["importo"] == 1000.0
    det = test_client.get(f"/api/customers/{cust.id}").json()
    assert next(i for i in det["invoices"]["items"] if i["id"] == a.id)["in_incasso"] is False


def test_evoluzione_exposes_in_incasso(test_client, test_db_session, okasan):
    from backend.engine.overdue_history import record_overdue_snapshot
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    record_overdue_snapshot(test_db_session)
    test_db_session.commit()
    ev = test_client.get("/api/dashboard/evoluzione").json()
    pts = ev.get("serie") or ev.get("points") or ev.get("items") or []
    assert pts, ev.keys()
    last = pts[-1]
    assert last.get("in_incasso") == 1000.0
    assert last.get("recuperato_assegni") == 1000.0


def test_reregister_preserves_registration_date(test_client, test_db_session, okasan):
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={"note": "prima"})
    test_db_session.refresh(a)
    t0 = a.payment_pending_at
    test_client.post(f"/api/positions/{a.id}/assegno", json={"note": "nota corretta"})
    test_db_session.refresh(a)
    assert a.payment_pending_at == t0 and a.payment_pending_note == "nota corretta"


def test_assegno_requires_overdue(test_client, test_db_session, okasan):
    cust, a, b, case = okasan
    c = Invoice(invoice_number="FT-C", amount=300.0, amount_due=300.0,
                issue_date=date.today() - timedelta(days=5), due_date=date.today() + timedelta(days=25),
                days_overdue=-25, status="open", customer_id=cust.id, source_platform="fatturapro")
    test_db_session.add(c); test_db_session.commit()
    assert test_client.post(f"/api/positions/{c.id}/assegno", json={}).status_code == 409


def test_disputed_after_check_not_recovered(test_client, test_db_session, okasan):
    """M4 econ: contestata dopo l'assegno → non conta come recuperato."""
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    test_client.put(f"/api/positions/{a.id}/status", params={"new_status": "disputed"})
    rec = test_client.get("/api/dashboard/riconciliazione").json()
    assert rec["recuperato"]["in_incasso_assegni"]["importo"] == 0.0
    assert rec["cascata"]["contestati"]["importo"] == 1000.0


def test_reopened_by_fatturapro_is_flagged_suspect(test_client, test_db_session, okasan):
    """M2 econ: pagata con assegno, confermata pagata dal sync, poi RIAPERTA su
    FatturaPro → sospetto insoluto segnalato (non marcato): cruscotto + 'Insoluto'
    ammesso per confermare."""
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    # simulo il sync: pagata (chiude lo stato assegno) poi riaperta
    a.status = "paid"; a.payment_pending = None; a.bounced_at = None
    test_db_session.commit()
    a.status = "open"; a.paid_at = None
    test_db_session.commit()
    dash = test_client.get("/api/dashboard").json()
    assert dash["assegni"]["sospetti"]["fatture"] == 1
    assert is_overdue_unpaid(a)  # è tornata lavorabile
    r = test_client.post(f"/api/positions/{a.id}/assegno/insoluto")
    assert r.status_code == 200 and r.json()["bounced_at"] is not None


def test_sollecito_rejects_in_incasso(test_client, okasan):
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    r = test_client.post(f"/api/recovery/customers/{cust.id}/solleciti",
                         json={"invoice_ids": [a.id], "channel": "whatsapp_copy"})
    assert r.status_code == 400


def test_case_closes_with_in_incasso_reason(test_client, test_db_session, okasan):
    cust, a, b, case = okasan
    test_client.post(f"/api/positions/{a.id}/assegno", json={})
    test_client.post(f"/api/positions/{b.id}/assegno", json={})
    test_db_session.refresh(case)
    assert case.status == "closed" and case.closed_reason == "in_incasso"


def test_suspect_dismiss_and_count(test_client, test_db_session, okasan):
    """Seconda review: il sospetto si SMENTISCE (Non è insoluto) e il conteggio
    dei sospetti non è limitato a 20."""
    cust, a, b, case = okasan
    today = date.today()
    for k in range(21):
        inv = Invoice(invoice_number=f"FT-S{k}", amount=10.0, amount_due=10.0,
                      issue_date=today - timedelta(days=60), due_date=today - timedelta(days=30),
                      days_overdue=30, status="open", customer_id=cust.id, source_platform="fatturapro",
                      payment_pending=None, payment_pending_at=datetime.utcnow() - timedelta(days=5))
        test_db_session.add(inv)
    test_db_session.commit()
    dash = test_client.get("/api/dashboard").json()
    assert dash["assegni"]["sospetti"]["fatture"] == 21
    sid = test_db_session.query(Invoice).filter_by(invoice_number="FT-S0").first().id
    det = test_client.get(f"/api/customers/{cust.id}").json()
    assert next(i for i in det["invoices"]["items"] if i["id"] == sid)["suspect_bounce"] is True
    r = test_client.delete(f"/api/positions/{sid}/assegno")
    assert r.status_code == 200
    assert test_client.get("/api/dashboard").json()["assegni"]["sospetti"]["fatture"] == 20


def test_assegno_rejects_orphan(test_client, test_db_session):
    inv = Invoice(invoice_number="FT-ORF", amount=100.0, amount_due=100.0,
                  issue_date=date.today() - timedelta(days=60), due_date=date.today() - timedelta(days=30),
                  days_overdue=30, status="open", customer_id=None, source_platform="fatturapro")
    test_db_session.add(inv); test_db_session.commit()
    assert test_client.post(f"/api/positions/{inv.id}/assegno", json={}).status_code == 409
