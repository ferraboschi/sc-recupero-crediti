"""Tono/numerazione PER-FATTURA (Fase 2 ②).

- il numero del sollecito è quello delle fatture citate (stadio più basso+1):
  la fattura nuova riceve il SUO 1° anche se il cliente è al 2° su altre;
- stato cliente = rollup "più urgente" per-fattura (non retrocede);
- dedup giornaliero PER STADIO: 1° (nuova) e 2° (vecchie) lo stesso giorno
  sono due solleciti distinti; stesso stadio → merge;
- 'lawyer' solo se TUTTE le scadute sono consegnate; un todo legale
  pendente non basta; rete legacy senza righe di join.
"""
from datetime import date, datetime, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryAction
from backend.engine.cases import ensure_open_case, _refresh_customer_status
from backend.engine.action_invoices import set_action_invoices


@pytest.fixture
def giugno_settembre(test_db_session):
    """2 fatture di giugno al 2° sollecito + 1 nuova di settembre a 0."""
    cust = Customer(ragione_sociale="Giugno Settembre S.R.L.")
    test_db_session.add(cust)
    test_db_session.commit()
    today = date.today()
    invs = []
    for n, days in (("FT-GIU1", 90), ("FT-GIU2", 80), ("FT-SET", 3)):
        inv = Invoice(
            invoice_number=n, amount=500.0, amount_due=500.0,
            issue_date=today - timedelta(days=days + 30),
            due_date=today - timedelta(days=days), days_overdue=days,
            status="open", customer_id=cust.id, source_platform="fatturapro",
        )
        test_db_session.add(inv)
        invs.append(inv)
    test_db_session.commit()
    case = ensure_open_case(test_db_session, cust)
    old = [invs[0].id, invs[1].id]
    for k, atype in enumerate(("first_contact", "second_contact")):
        when = datetime.utcnow() - timedelta(days=30 - 10 * k)
        a = RecoveryAction(
            customer_id=cust.id, case_id=case.id, action_type=atype,
            completed_at=when, created_at=when, outcome="contacted",
            channel="whatsapp_copy", invoice_ids=old,
        )
        test_db_session.add(a)
        test_db_session.flush()
        set_action_invoices(test_db_session, a.id, old)
    cust.recovery_status = "second_contact"
    test_db_session.commit()
    return cust, invs


def _post(client, cust_id, ids):
    return client.post(f"/api/recovery/customers/{cust_id}/solleciti",
                       json={"invoice_ids": ids, "channel": "whatsapp_copy"}).json()


def test_new_invoice_gets_its_own_first(test_client, test_db_session, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    r = _post(test_client, cust.id, [s.id])
    assert r["registered"] and r["sollecito_n"] == 1
    act = test_db_session.query(RecoveryAction).get(r["action_id"])
    assert act.action_type == "first_contact"
    # rollup: le vecchie sono al 2° → il cliente NON retrocede
    test_db_session.refresh(cust)
    assert cust.recovery_status == "second_contact"


def test_old_invoice_gets_third(test_client, test_db_session, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    r = _post(test_client, cust.id, [g1.id])
    assert r["sollecito_n"] == 3
    act = test_db_session.query(RecoveryAction).get(r["action_id"])
    assert act.action_type == "second_contact"


def test_mixed_group_uses_gentlest(test_client, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    r = _post(test_client, cust.id, [g1.id, s.id])
    assert r["sollecito_n"] == 1


def test_same_day_two_stages_are_two_solleciti(test_client, test_db_session, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    r1 = _post(test_client, cust.id, [s.id])       # 1° per la nuova
    r2 = _post(test_client, cust.id, [g1.id])      # 2°(+) per la vecchia
    assert not r2.get("already_registered_today")
    assert r1["action_id"] != r2["action_id"]
    r3 = _post(test_client, cust.id, [g2.id])      # stesso stadio di r2 → merge
    assert r3.get("already_registered_today") is True
    assert r3["action_id"] == r2["action_id"]
    act = test_db_session.query(RecoveryAction).get(r2["action_id"])
    assert sorted(act.invoice_ids) == sorted([g1.id, g2.id])


def test_rollup_lawyer_only_when_all_delivered(test_client, test_db_session, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    # todo legale PENDENTE: non è "dal legale"
    test_db_session.add(RecoveryAction(customer_id=cust.id, case_id=case.id,
                                       action_type="lawyer", scheduled_date=date.today()))
    test_db_session.commit()
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "second_contact"
    # consegna parziale (le due vecchie) → resta second_contact
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                     json={"invoice_ids": [g1.id, g2.id]})
    test_db_session.refresh(cust)
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "second_contact"
    # consegna anche la nuova → lawyer
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                     json={"invoice_ids": [s.id]})
    test_db_session.refresh(cust)
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "lawyer"


def test_rollup_legacy_without_join_rows(test_db_session):
    """Pratica con 2 contatti ma NESSUNA riga di join → si usa il contatore
    di pratica (non retrocede a idle)."""
    cust = Customer(ragione_sociale="Legacy S.R.L.")
    test_db_session.add(cust)
    test_db_session.commit()
    inv = Invoice(invoice_number="FT-L", amount=100.0, amount_due=100.0,
                  issue_date=date.today() - timedelta(days=60),
                  due_date=date.today() - timedelta(days=30), days_overdue=30,
                  status="open", customer_id=cust.id, source_platform="fatturapro")
    test_db_session.add(inv)
    test_db_session.commit()
    case = ensure_open_case(test_db_session, cust)
    for atype in ("first_contact", "second_contact"):
        test_db_session.add(RecoveryAction(customer_id=cust.id, case_id=case.id,
                                           action_type=atype, completed_at=datetime.utcnow()))
    test_db_session.commit()
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "second_contact"


def test_recopy_same_invoice_same_day_does_not_escalate(test_client, test_db_session, giugno_settembre):
    """Ri-copiare lo stesso messaggio (stessa fattura) nello stesso giorno =
    stesso sollecito: la fattura NON sale di stadio in un giorno."""
    from backend.engine.action_invoices import per_invoice_sollecito_stats
    cust, (g1, g2, s) = giugno_settembre
    r1 = _post(test_client, cust.id, [s.id])
    r2 = _post(test_client, cust.id, [s.id])
    assert r2.get("already_registered_today") is True
    assert r2["action_id"] == r1["action_id"]
    assert r2["sollecito_n"] == 1
    assert per_invoice_sollecito_stats(test_db_session, [s.id])[s.id]["count"] == 1


def test_inherited_contacts_keep_tone(test_client, test_db_session, giugno_settembre):
    """Contatti ereditati da una pratica archiviata: il tono non riparte mai
    cordiale (regola owner) — si sommano allo stadio della fattura."""
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    case.inherited_contacts = 2
    test_db_session.commit()
    r = _post(test_client, cust.id, [s.id])
    assert r["sollecito_n"] == 3
    act = test_db_session.query(RecoveryAction).get(r["action_id"])
    assert act.action_type == "second_contact"


def test_cross_stage_recopy_splits_not_merges(test_client, test_db_session, giugno_settembre):
    """Ri-copy della nuova + vecchia insieme: la nuova (già citata oggi) non
    si riconta; la vecchia va nel SUO stadio in un'azione separata."""
    cust, (g1, g2, s) = giugno_settembre
    r1 = _post(test_client, cust.id, [s.id])
    r2 = _post(test_client, cust.id, [s.id, g1.id])
    assert r2["action_id"] != r1["action_id"]
    assert r2["sollecito_n"] == 3
    a2 = test_db_session.query(RecoveryAction).get(r2["action_id"])
    assert a2.action_type == "second_contact" and a2.invoice_ids == [g1.id]
    a1 = test_db_session.query(RecoveryAction).get(r1["action_id"])
    assert a1.invoice_ids == [s.id]


def test_undo_restores_rollup(test_client, test_db_session, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    g1.status = "paid"; g2.status = "paid"
    test_db_session.commit()
    r = _post(test_client, cust.id, [s.id])
    test_db_session.refresh(cust)
    assert cust.recovery_status == "first_contact"
    test_client.delete(f"/api/recovery/customers/{cust.id}/solleciti/{r['action_id']}")
    test_db_session.refresh(cust)
    assert cust.recovery_status == "idle"


def test_fallback_not_applied_when_reminded_invoices_paid(test_db_session, giugno_settembre):
    """Le sollecitate (collegate) sono pagate, resta solo la nuova mai
    sollecitata: niente rete legacy → idle, non second_contact."""
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    g1.status = "paid"; g2.status = "paid"
    test_db_session.commit()
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "idle"


def test_customer_detail_sollecito_today(test_client, giugno_settembre):
    cust, (g1, g2, s) = giugno_settembre
    _post(test_client, cust.id, [s.id])
    det = test_client.get(f"/api/customers/{cust.id}").json()
    by = {i["id"]: i for i in det["invoices"]["items"]}
    assert by[s.id]["sollecito_today"] is True and by[g1.id]["sollecito_today"] is False


def test_unlinked_manual_contact_keeps_tone(test_client, test_db_session, giugno_settembre):
    """Contatto completato SENZA fatture collegate (storico): la numerazione
    non ricomincia cordiale (rete legacy sulla numerazione)."""
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    test_db_session.add(RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="first_contact",
                                       completed_at=datetime.utcnow() - timedelta(days=5)))
    test_db_session.commit()
    r = _post(test_client, cust.id, [s.id])
    assert r["sollecito_n"] >= 2
    det = test_client.get(f"/api/customers/{cust.id}").json()
    assert det["case"]["has_unlinked_contacts"] is True


def test_stale_lawyer_todo_cancelled_when_invoices_paid(test_client, test_db_session, giugno_settembre):
    """Todo legale pianificato per le vecchie; le vecchie vengono pagate; resta
    solo la nuova (0 solleciti): il todo è stantio → annullato, progressione
    normale (prossima azione = 2° contatto, non avvocato)."""
    from backend.engine.cases import schedule_next_action
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    todo = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="lawyer",
                          scheduled_date=date.today() - timedelta(days=2),
                          notes="Auto-pianificata dopo il 2° contatto")
    test_db_session.add(todo)
    g1.status = "paid"; g2.status = "paid"
    test_db_session.commit()
    r = _post(test_client, cust.id, [s.id])
    assert r["sollecito_n"] == 1
    test_db_session.refresh(todo)
    assert todo.cancelled is True
    test_db_session.refresh(cust)
    assert cust.next_action_type == "second_contact"


def test_resplit_marks_moved_customers(test_db_session, giugno_settembre, monkeypatch):
    from backend.engine import cases as cases_mod
    from backend.database import ActivityLog
    cust, (g1, g2, s) = giugno_settembre
    cust.recovery_status = "lawyer"  # stato stantio (nessuna consegna)
    test_db_session.commit()
    from backend.database import SyncState
    test_db_session.add(SyncState(key="action_invoices_backfill", result={"done": True}))
    test_db_session.commit()
    monkeypatch.setattr("backend.database.get_session_direct", lambda: test_db_session)
    # get_session_direct è importato lazy dentro la funzione → patchare il modulo database
    orig_close = test_db_session.close
    monkeypatch.setattr(test_db_session, "close", lambda: None)
    res = cases_mod.resplit_status_if_needed()
    assert res and res["moved"] >= 1
    test_db_session.refresh(cust)
    assert cust.recovery_status == "second_contact"
    logs = test_db_session.query(ActivityLog).filter_by(action="status_resplit").all()
    assert any(l.entity_id == cust.id and l.details["from"] == "lawyer" for l in logs)
    assert cases_mod.resplit_status_if_needed() == {"skipped": True}
    monkeypatch.setattr(test_db_session, "close", orig_close)


def test_followup_lawyer_todo_kept_while_delivered_unpaid(test_client, test_db_session, giugno_settembre):
    """B1: le vecchie sono state CONSEGNATE (impagate) e c'è il follow-up
    avvocato; un 1° sollecito sulla nuova NON lo annulla."""
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover", json={"invoice_ids": [g1.id, g2.id]})
    fu = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="lawyer",
                        scheduled_date=date.today() + timedelta(days=20),
                        notes="Auto-pianificata: follow-up avvocato")
    test_db_session.add(fu); test_db_session.commit()
    _post(test_client, cust.id, [s.id])
    test_db_session.refresh(fu)
    assert fu.cancelled is not True
    test_db_session.refresh(cust)
    assert cust.next_action_type == "lawyer"


def test_complete_action_uses_per_invoice_n(test_client, test_db_session, giugno_settembre):
    """M1: completare un contatto manuale da Attività non riporta il contatore
    di pratica nella progressione: n per-fattura e nessun todo legale doppio."""
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    g1.status = "paid"; g2.status = "paid"
    stale = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="lawyer",
                           scheduled_date=date.today() - timedelta(days=1),
                           notes="Auto-pianificata dopo il 2° contatto")
    todo = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="first_contact",
                          scheduled_date=date.today())
    test_db_session.add_all([stale, todo]); test_db_session.commit()
    r = test_client.put(f"/api/recovery/customers/{cust.id}/actions/{todo.id}/complete",
                        params={"outcome": "contacted"})
    assert r.status_code == 200
    test_db_session.refresh(stale)
    assert stale.cancelled is True and stale.cancelled_reason == f"superseded_by_sollecito:{todo.id}"
    lawyer_todos = test_db_session.query(RecoveryAction).filter_by(
        case_id=case.id, action_type="lawyer", completed_at=None, cancelled=False).count()
    assert lawyer_todos == 0
    test_db_session.refresh(cust)
    assert cust.recovery_status == "first_contact" and cust.next_action_type == "second_contact"


def test_explicit_empty_cited_does_not_poison(test_db_session, giugno_settembre):
    """M2: un contatto con invoice_ids=[] esplicito non è 'legacy'."""
    from backend.engine.cases import _has_unlinked_contacts
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    test_db_session.add(RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="first_contact",
                                       completed_at=datetime.utcnow(), invoice_ids=[]))
    test_db_session.commit()
    assert _has_unlinked_contacts(test_db_session, case) is False


def test_undo_restores_stale_cancelled_todo(test_client, test_db_session, giugno_settembre):
    """M3: l'undo del sollecito ripristina il todo legale che aveva annullato."""
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    g1.status = "paid"; g2.status = "paid"
    stale = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="lawyer",
                           scheduled_date=date.today() - timedelta(days=1),
                           notes="Auto-pianificata dopo il 2° contatto")
    test_db_session.add(stale); test_db_session.commit()
    r = _post(test_client, cust.id, [s.id])
    test_db_session.refresh(stale)
    assert stale.cancelled is True
    u = test_client.delete(f"/api/recovery/customers/{cust.id}/solleciti/{r['action_id']}").json()
    assert u["restored_pending"] >= 1
    test_db_session.refresh(stale)
    assert stale.cancelled is False


def test_resplit_waits_for_join_backfill(test_db_session, giugno_settembre, monkeypatch):
    """M4: senza il marker del backfill azione↔fattura il resplit si rimanda."""
    from backend.engine import cases as cases_mod
    monkeypatch.setattr("backend.database.get_session_direct", lambda: test_db_session)
    monkeypatch.setattr(test_db_session, "close", lambda: None)
    assert cases_mod.resplit_status_if_needed() == {"skipped": "waiting_join_backfill"}


def test_rollup_keeps_operator_wait(test_db_session, giugno_settembre):
    """Un todo di ATTESA pendente (scelta dell'operatore) non viene
    sovrascritto dal rollup per-fattura."""
    cust, (g1, g2, s) = giugno_settembre
    case = ensure_open_case(test_db_session, cust)
    test_db_session.add(RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="wait",
                                       scheduled_date=date.today() + timedelta(days=10)))
    test_db_session.commit()
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "waiting"
    assert cust.next_action_type == "wait"
