"""Fase 4 — stadio per fattura e gruppi per stadio con la loro storia."""
from datetime import date, datetime, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryAction
from backend.engine.cases import ensure_open_case
from backend.engine.action_invoices import set_action_invoices, per_invoice_actions
from backend.engine.stages import build_stage_groups, invoice_stage


@pytest.fixture
def ferro(test_db_session):
    cust = Customer(ragione_sociale="Ferro Distribuzione S.R.L.", phone="+39 333 0000000")
    test_db_session.add(cust); test_db_session.commit()
    today = date.today()
    invs = []
    for n, days in (("FT-OLD1", 130), ("FT-OLD2", 110), ("FT-MID", 60), ("FT-NEW", 2)):
        inv = Invoice(invoice_number=n, amount=1000.0, amount_due=1000.0,
                      issue_date=today - timedelta(days=days + 30), due_date=today - timedelta(days=days),
                      days_overdue=days, status="open", customer_id=cust.id, source_platform="fatturapro")
        test_db_session.add(inv); invs.append(inv)
    test_db_session.commit()
    case = ensure_open_case(test_db_session, cust)
    old1, old2, mid, new = invs
    # 1° sollecito su OLD1, OLD2, MID; 2° solo su OLD1, OLD2
    for k, (atype, ids) in enumerate((("first_contact", [old1.id, old2.id, mid.id]),
                                      ("second_contact", [old1.id, old2.id]))):
        when = datetime.utcnow() - timedelta(days=40 - 15 * k)
        a = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type=atype,
                           completed_at=when, created_at=when, outcome="contacted",
                           channel="whatsapp_copy", invoice_ids=ids, notes=f"Sollecito n. {k+1}")
        test_db_session.add(a); test_db_session.flush()
        set_action_invoices(test_db_session, a.id, ids)
    test_db_session.commit()
    return cust, invs, case


def test_stage_per_invoice_and_groups(test_db_session, ferro):
    cust, (old1, old2, mid, new), case = ferro
    info = build_stage_groups(test_db_session, cust, case)
    assert info["invoices"] == {old1.id: "second", old2.id: "second", mid.id: "first", new.id: "none"}
    stages = [g["stage"] for g in info["groups"]]
    assert stages == ["none", "first", "second"]        # ordine del flusso (allarmi in testa)
    g2 = info["groups"][2]
    assert sorted(g2["invoice_ids"]) == sorted([old1.id, old2.id]) and g2["total"] == 2000.0
    assert g2["tone"] == "second"
    # storia del gruppo: 1° (citava 3 fatture, 2 di questo gruppo) + 2°
    labels = [(r["label"], r["cited_in_group"], r["cited_total"]) for r in g2["actions"]]
    assert labels == [("1° sollecito", 2, 3), ("2° sollecito", 2, 2)]
    assert g2["next_n"] == 3
    g1 = info["groups"][1]
    assert g1["invoice_ids"] == [mid.id] and g1["tone"] == "second" and g1["next_n"] == 2
    assert [(r["label"], r["cited_in_group"]) for r in g1["actions"]] == [("1° sollecito", 1)]
    g0 = info["groups"][0]
    assert g0["invoice_ids"] == [new.id] and g0["tone"] == "first" and g0["actions"] == []


def test_lawyer_and_check_stages(test_client, test_db_session, ferro):
    cust, (old1, old2, mid, new), case = ferro
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover", json={"invoice_ids": [old1.id]})
    test_client.post(f"/api/positions/{mid.id}/assegno", json={})
    det = test_client.get(f"/api/customers/{cust.id}").json()
    by = {i["id"]: i["stage"] for i in det["invoices"]["items"]}
    assert by[old1.id] == "lawyer" and by[old2.id] == "second"
    assert by[mid.id] == "in_incasso" and by[new.id] == "none"
    stages = [g["stage"] for g in det["stage_groups"]]
    assert stages == ["none", "second", "lawyer", "in_incasso"]
    lawyer_group = next(g for g in det["stage_groups"] if g["stage"] == "lawyer")
    assert any(r["action_type"] == "lawyer" and r["cited_in_group"] == 1 for r in lawyer_group["actions"])
    assert next(i for i in det["invoices"]["items"] if i["id"] == old1.id)["stage_label"] == "Consegnata all'avvocato"


def test_group_note_cites_invoices_and_travels(test_client, test_db_session, ferro):
    cust, (old1, old2, mid, new), case = ferro
    r = test_client.post(f"/api/recovery/customers/{cust.id}/actions",
                         json={"action_type": "note", "notes": "Promette bonifico entro il 15",
                               "invoice_ids": [old1.id, old2.id]})
    assert r.status_code == 200, r.text
    det = test_client.get(f"/api/customers/{cust.id}").json()
    g2 = next(g for g in det["stage_groups"] if g["stage"] == "second")
    assert any(a["action_type"] == "note" and a["notes"].startswith("Promette") and a["cited_in_group"] == 2
               for a in g2["actions"])
    g0 = next(g for g in det["stage_groups"] if g["stage"] == "none")
    assert not any(a["action_type"] == "note" for a in g0["actions"])
    # viaggia fino al dossier (per_invoice_actions include le note)
    acts = per_invoice_actions(test_db_session, [old1.id])
    assert any(a.action_type == "note" for a in acts[old1.id])
    # fatture estranee → 400
    assert test_client.post(f"/api/recovery/customers/{cust.id}/actions",
                            json={"action_type": "note", "notes": "x", "invoice_ids": [999999]}).status_code == 400


def test_edit_action_notes(test_client, test_db_session, ferro):
    cust, invs, case = ferro
    a = test_db_session.query(RecoveryAction).filter_by(customer_id=cust.id, action_type="first_contact").first()
    r = test_client.put(f"/api/recovery/customers/{cust.id}/actions/{a.id}/notes",
                        json={"notes": "Parlato con la titolare: paga il 20"})
    assert r.status_code == 200 and r.json()["notes"].startswith("Parlato")
    test_db_session.refresh(a)
    assert a.notes.startswith("Parlato")
    assert test_client.put(f"/api/recovery/customers/{cust.id}/actions/999999/notes", json={"notes": "x"}).status_code == 404


def test_client_actions_and_pending(test_client, test_db_session, ferro):
    cust, invs, case = ferro
    test_client.post(f"/api/recovery/customers/{cust.id}/actions", json={"action_type": "wait", "notes": "aspetto"})
    det = test_client.get(f"/api/customers/{cust.id}").json()
    assert any(p["action_type"] == "wait" for p in det["pending_actions"])


def test_legacy_action_scoped_by_due_date_and_case(test_db_session, ferro):
    """Un'azione legacy (nessuna fattura citata né righe di join) copriva le
    scadute ALL'EPOCA: compare solo nei gruppi con fatture già scadute a quella
    data, mai su quelle nate dopo; e solo se della pratica corrente. (Con un
    contatto legacy nella pratica il tono non riparte cordiale: NEW → 'first'.)"""
    cust, (old1, old2, mid, new), case = ferro
    when = datetime.utcnow() - timedelta(days=100)   # OLD1/OLD2 (130/110gg) già scadute; MID (60gg) e NEW no
    test_db_session.add(RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="first_contact",
                                       completed_at=when, created_at=when, invoice_ids=None))
    other = RecoveryAction(customer_id=cust.id, case_id=None, action_type="second_contact",
                           completed_at=when, created_at=when, invoice_ids=None)
    test_db_session.add(other); test_db_session.commit()
    info = build_stage_groups(test_db_session, cust, case)
    assert info["invoices"][new.id] == "first"        # rete legacy: mai più cordiale
    by = {g["stage"]: g for g in info["groups"]}
    # nel gruppo 'second' (OLD1, OLD2, MID): le legacy citano solo OLD1/OLD2 (2 di 3)
    legacy_rows = [r for r in by["second"]["actions"] if r["legacy_all"]]
    assert len(legacy_rows) == 2 and all(r["cited_in_group"] == 2 for r in legacy_rows)
    # nel gruppo 'first' (NEW, nata dopo): nessuna legacy
    assert not any(r["legacy_all"] for r in by["first"]["actions"])
    # un'azione di un'ALTRA pratica non compare
    other.case_id = 999999; test_db_session.commit()
    info = build_stage_groups(test_db_session, cust, case)
    by = {g["stage"]: g for g in info["groups"]}
    assert sum(1 for r in by["second"]["actions"] if r["legacy_all"]) == 1


def test_disputed_excluded_and_in_incasso_not_overdue_included(test_client, test_db_session, ferro):
    cust, (old1, old2, mid, new), case = ferro
    new.status = "disputed"
    test_client.post(f"/api/positions/{mid.id}/assegno", json={})
    mid.days_overdue = 0; mid.due_date = date.today() + timedelta(days=10)
    test_db_session.commit()
    det = test_client.get(f"/api/customers/{cust.id}").json()
    by = {i["id"]: i["stage"] for i in det["invoices"]["items"]}
    assert by[new.id] is None                      # contestata: fuori dai gruppi
    assert by[mid.id] == "in_incasso"              # non scaduta ma in incasso: resta gestibile
    assert [g["stage"] for g in det["stage_groups"]] == ["second", "in_incasso"]


def test_tone_from_count_even_for_insoluto(test_client, test_db_session, ferro):
    """Insoluto su fattura MAI sollecitata: il tono è cordiale (1°), coerente
    con la numerazione del backend (n=1)."""
    cust, (old1, old2, mid, new), case = ferro
    test_client.post(f"/api/positions/{new.id}/assegno", json={})
    test_client.post(f"/api/positions/{new.id}/assegno/insoluto")
    det = test_client.get(f"/api/customers/{cust.id}").json()
    g = next(g for g in det["stage_groups"] if g["stage"] == "insoluto")
    assert g["tone"] == "first"


def test_group_flags_today_and_recopy_tone(test_client, test_db_session, ferro):
    cust, (old1, old2, mid, new), case = ferro
    r = test_client.post(f"/api/recovery/customers/{cust.id}/solleciti",
                         json={"invoice_ids": [new.id], "channel": "whatsapp_copy"})
    assert r.json()["sollecito_n"] == 1
    det = test_client.get(f"/api/customers/{cust.id}").json()
    g = next(g for g in det["stage_groups"] if new.id in g["invoice_ids"])
    inv = next(i for i in g["invoices"] if i["id"] == new.id)
    assert g["stage"] == "first" and inv["sollecito_today"] is True and inv["recopy_tone"] == "first"


def test_notes_edit_refused_on_cancelled(test_client, test_db_session, ferro):
    cust, invs, case = ferro
    a = test_db_session.query(RecoveryAction).filter_by(customer_id=cust.id, action_type="first_contact").first()
    a.cancelled = True; test_db_session.commit()
    assert test_client.put(f"/api/recovery/customers/{cust.id}/actions/{a.id}/notes", json={"notes": "x"}).status_code == 409
