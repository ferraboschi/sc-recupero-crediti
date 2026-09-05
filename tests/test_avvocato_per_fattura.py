"""Avvocato PER-FATTURA (Fase 2 ③, decisione owner: si consegnano le fatture
che l'operatore SCEGLIE).

- candidati: lista fatture con sollecito_count e flag consegnata;
- handover parziale: il cliente RESTA candidato per le fatture non consegnate
  (caso Ferro), stato non ancora legale; completo → stato legale, esce;
- handover: fatture estranee → 400; già consegnate → idempotente; body vuoto
  = tutte le non consegnate (legacy);
- dossier-zip con invoice_ids: solo quelle; estranee → 400;
- dossier-zip-all: selezione di default = non consegnate con ≥2 solleciti.
"""
import io
import zipfile
from datetime import date, datetime, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryAction
from backend.engine.cases import ensure_open_case
from backend.engine.action_invoices import set_action_invoices


@pytest.fixture
def ferro(test_db_session):
    """Ferro: 2 fatture VECCHIE con 2 solleciti ciascuna + 1 NUOVA a 0.
    Debito 3000 > 1500, 2 contatti freschi nel ciclo → candidato."""
    cust = Customer(ragione_sociale="Ferro Distribuzione S.R.L.", partita_iva="01234567890")
    test_db_session.add(cust)
    test_db_session.commit()
    today = date.today()
    invs = []
    for n, days in (("FT-OLD1", 130), ("FT-OLD2", 110), ("FT-NEW", 2)):
        inv = Invoice(
            invoice_number=n, amount=1000.0, amount_due=1000.0,
            issue_date=today - timedelta(days=days + 30),
            due_date=today - timedelta(days=days), days_overdue=days,
            status="open", customer_id=cust.id, source_platform="fatturapro",
        )
        test_db_session.add(inv)
        invs.append(inv)
    test_db_session.commit()
    case = ensure_open_case(test_db_session, cust)
    old_ids = [invs[0].id, invs[1].id]
    for k, atype in enumerate(("first_contact", "second_contact")):
        when = datetime.utcnow() - timedelta(days=40 - 15 * k)
        a = RecoveryAction(
            customer_id=cust.id, case_id=case.id, action_type=atype,
            completed_at=when, created_at=when, outcome="contacted",
            channel="whatsapp_copy", invoice_ids=old_ids,
        )
        test_db_session.add(a)
        test_db_session.flush()
        set_action_invoices(test_db_session, a.id, old_ids)
    test_db_session.commit()
    return cust, invs


def _item(client, cust_id):
    items = client.get("/api/avvocato/candidates").json()["items"]
    return next((i for i in items if i["id"] == cust_id), None)


def test_candidates_expose_per_invoice(test_client, ferro):
    cust, (old1, old2, new) = ferro
    it = _item(test_client, cust.id)
    assert it is not None
    by = {i["id"]: i for i in it["invoices"]}
    assert set(by) == {old1.id, old2.id, new.id}
    assert by[old1.id]["sollecito_count"] == 2
    assert by[old2.id]["sollecito_count"] == 2
    assert by[new.id]["sollecito_count"] == 0
    assert not any(i["delivered"] for i in by.values())
    assert it["undelivered_total"] == 3000.0


def test_partial_handover_keeps_candidate(test_client, test_db_session, ferro):
    cust, (old1, old2, new) = ferro
    r = test_client.post(
        f"/api/avvocato/customers/{cust.id}/handover",
        json={"invoice_ids": [old1.id, old2.id]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["already"] is False
    assert sorted(body["delivered"]) == sorted([old1.id, old2.id])
    assert body["remaining"] == 1
    assert body["recovery_status"] != "lawyer"  # la nuova resta in sollecito
    # resta candidato con la sola nuova da consegnare
    it = _item(test_client, cust.id)
    assert it is not None
    by = {i["id"]: i for i in it["invoices"]}
    assert by[old1.id]["delivered"] and by[old2.id]["delivered"]
    assert not by[new.id]["delivered"]
    assert it["undelivered_total"] == 1000.0
    # l'azione lawyer cita le due fatture
    act = test_db_session.query(RecoveryAction).filter_by(
        customer_id=cust.id, action_type="lawyer").first()
    assert sorted(act.invoice_ids) == sorted([old1.id, old2.id])


def test_full_handover_sets_lawyer_and_removes(test_client, ferro):
    cust, (old1, old2, new) = ferro
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                     json={"invoice_ids": [old1.id, old2.id]})
    r = test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                         json={"invoice_ids": [new.id]})
    assert r.status_code == 200
    assert r.json()["remaining"] == 0
    assert r.json()["recovery_status"] == "lawyer"
    assert _item(test_client, cust.id) is None


def test_handover_already_delivered_idempotent(test_client, ferro):
    cust, (old1, old2, new) = ferro
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                     json={"invoice_ids": [old1.id]})
    r = test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                         json={"invoice_ids": [old1.id]})
    assert r.status_code == 200 and r.json()["already"] is True


def test_handover_unknown_invoice_400(test_client, ferro):
    cust, _ = ferro
    r = test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                         json={"invoice_ids": [999999]})
    assert r.status_code == 400


def test_handover_empty_body_delivers_all(test_client, ferro):
    cust, invs = ferro
    r = test_client.post(f"/api/avvocato/customers/{cust.id}/handover")
    assert r.status_code == 200
    assert r.json()["remaining"] == 0
    assert r.json()["recovery_status"] == "lawyer"


def _zip_names(content):
    return zipfile.ZipFile(io.BytesIO(content)).namelist()


def test_dossier_zip_selected_only(test_client, ferro):
    cust, (old1, old2, new) = ferro
    r = test_client.get(f"/api/avvocato/customers/{cust.id}/dossier-zip",
                        params={"invoice_ids": f"{old1.id}"})
    assert r.status_code == 200
    names = _zip_names(r.content)
    fatt = [n for n in names if n.startswith("fatture/")]
    assert len(fatt) == 1 and "FT-OLD1" in fatt[0]
    assert any(n.startswith("dossier_") for n in names)


def test_dossier_zip_unknown_400(test_client, ferro):
    cust, _ = ferro
    r = test_client.get(f"/api/avvocato/customers/{cust.id}/dossier-zip",
                        params={"invoice_ids": "999999"})
    assert r.status_code == 400


def test_dossier_zip_all_default_selection(test_client, ferro):
    cust, (old1, old2, new) = ferro
    r = test_client.get("/api/avvocato/dossier-zip-all")
    assert r.status_code == 200
    names = _zip_names(r.content)
    fatt = [n for n in names if "/fatture/" in n]
    # solo le 2 mature (≥2 solleciti), NON la nuova
    assert len(fatt) == 2
    assert not any("FT-NEW" in n for n in fatt)


# ── Fix review avversariale ─────────────────────────────────────────

def test_legacy_handover_scoped_by_due_date(test_client, test_db_session, ferro):
    """Handover LEGACY (pre-tabella, senza fatture citate) 60gg fa: consegna le
    fatture già scadute ALLORA; la nuova (scaduta dopo) NON risulta consegnata
    e il cliente resta candidato per lei (caso Ferro)."""
    cust, (old1, old2, new) = ferro
    case = ensure_open_case(test_db_session, cust)
    when = datetime.utcnow() - timedelta(days=60)
    test_db_session.add(RecoveryAction(
        customer_id=cust.id, case_id=case.id, action_type="lawyer",
        completed_at=when, created_at=when, outcome="handover", invoice_ids=None))
    test_db_session.commit()
    it = _item(test_client, cust.id)
    assert it is not None
    by = {i["id"]: i for i in it["invoices"]}
    assert by[old1.id]["delivered"] and by[old2.id]["delivered"]
    assert not by[new.id]["delivered"]


def test_handover_cancels_pending_lawyer_todo(test_client, test_db_session, ferro):
    cust, (old1, old2, new) = ferro
    case = ensure_open_case(test_db_session, cust)
    todo = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="lawyer",
                          scheduled_date=date.today() + timedelta(days=3))
    test_db_session.add(todo)
    test_db_session.commit()
    r = test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                         json={"invoice_ids": [old1.id]})
    assert r.status_code == 200 and r.json()["remaining"] == 2
    test_db_session.refresh(todo)
    assert todo.cancelled is True
    assert (todo.cancelled_reason or "").startswith("superseded_by_handover:")
    test_db_session.refresh(cust)
    assert cust.next_action_type is None  # nessun altro todo pendente


def test_handover_explicit_empty_400(test_client, ferro):
    cust, _ = ferro
    r = test_client.post(f"/api/avvocato/customers/{cust.id}/handover", json={"invoice_ids": []})
    assert r.status_code == 400


def test_complete_lawyer_todo_writes_invoices(test_client, test_db_session, ferro):
    """Flusso vecchio (todo legale completato da Attività): le fatture
    consegnate si scrivono ESPLICITAMENTE (invoice_ids + join)."""
    cust, (old1, old2, new) = ferro
    case = ensure_open_case(test_db_session, cust)
    todo = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="lawyer",
                          scheduled_date=date.today())
    test_db_session.add(todo)
    test_db_session.commit()
    r = test_client.put(f"/api/recovery/customers/{cust.id}/actions/{todo.id}/complete",
                        params={"outcome": "contacted"})
    assert r.status_code == 200, r.text
    test_db_session.refresh(todo)
    assert sorted(todo.invoice_ids) == sorted([old1.id, old2.id, new.id])
    assert _item(test_client, cust.id) is None  # tutto consegnato


def test_disputed_invoice_rejected(test_client, test_db_session, ferro):
    cust, (old1, old2, new) = ferro
    new.status = "disputed"
    test_db_session.commit()
    r = test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                         json={"invoice_ids": [new.id]})
    assert r.status_code == 400
    r = test_client.get(f"/api/avvocato/customers/{cust.id}/dossier-zip",
                        params={"invoice_ids": str(new.id)})
    assert r.status_code == 400


def test_zip_all_skips_client_without_mature(test_client, ferro):
    """Probe A: dopo la consegna delle due mature resta solo la nuova (0
    solleciti) → "Prepara tutti" NON la spedisce e lo dichiara in SALTATI.txt."""
    cust, (old1, old2, new) = ferro
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                     json={"invoice_ids": [old1.id, old2.id]})
    r = test_client.get("/api/avvocato/dossier-zip-all")
    assert r.status_code == 200
    names = _zip_names(r.content)
    assert not any("FT-NEW" in n for n in names)
    assert "SALTATI.txt" in names
    txt = zipfile.ZipFile(io.BytesIO(r.content)).read("SALTATI.txt").decode("latin-1")
    assert "Ferro" in txt


def test_followup_completion_does_not_deliver_later_invoice(test_client, test_db_session, ferro):
    """Probe C: dopo una consegna, completare un todo legale (follow-up) NON
    consegna la fattura scaduta in seguito."""
    cust, (old1, old2, new) = ferro
    case = ensure_open_case(test_db_session, cust)
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                     json={"invoice_ids": [old1.id, old2.id]})
    todo = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="lawyer",
                          scheduled_date=date.today(), notes="Auto-pianificata: follow-up avvocato")
    test_db_session.add(todo)
    test_db_session.commit()
    r = test_client.put(f"/api/recovery/customers/{cust.id}/actions/{todo.id}/complete",
                        params={"outcome": "contacted"})
    assert r.status_code == 200
    test_db_session.refresh(todo)
    assert todo.invoice_ids == []  # esplicito: nulla di nuovo
    it = _item(test_client, cust.id)
    assert it is not None
    assert not {i["id"]: i for i in it["invoices"]}[new.id]["delivered"]


def test_status_durable_after_partial_handover(test_client, test_db_session, ferro):
    """Probe D: dopo una consegna parziale, il refresh dello stato NON porta a
    'lawyer'; dopo la consegna completa sì."""
    from backend.engine.cases import _refresh_customer_status
    cust, (old1, old2, new) = ferro
    case = ensure_open_case(test_db_session, cust)
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                     json={"invoice_ids": [old1.id, old2.id]})
    test_db_session.refresh(cust)
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status != "lawyer"
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover",
                     json={"invoice_ids": [new.id]})
    test_db_session.refresh(cust)
    _refresh_customer_status(test_db_session, cust, case)
    assert cust.recovery_status == "lawyer"


def test_manual_contact_completion_cites_overdue(test_client, test_db_session, ferro):
    """Contatto registrato a mano (telefonata) completato da Attività: cita le
    scadute del momento → il conteggio per-fattura lo vede."""
    from backend.engine.action_invoices import per_invoice_sollecito_stats
    cust, (old1, old2, new) = ferro
    case = ensure_open_case(test_db_session, cust)
    todo = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="first_contact",
                          scheduled_date=date.today())
    test_db_session.add(todo)
    test_db_session.commit()
    r = test_client.put(f"/api/recovery/customers/{cust.id}/actions/{todo.id}/complete",
                        params={"outcome": "contacted"})
    assert r.status_code == 200, r.text
    test_db_session.refresh(todo)
    assert sorted(todo.invoice_ids) == sorted([old1.id, old2.id, new.id])
    st = per_invoice_sollecito_stats(test_db_session, [new.id])
    assert st[new.id]["count"] == 1
