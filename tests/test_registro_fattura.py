"""Fase 5 — la riga della fattura è il registro: canali Email/Telefono, vince
l'ultimo click, nota per fattura (riga + dossier), storia per fattura."""
from datetime import date, datetime, timedelta

import pytest

from backend.database import Customer, Invoice, RecoveryAction
from backend.engine.cases import ensure_open_case


@pytest.fixture
def cli(test_db_session):
    cust = Customer(ragione_sociale="Registro S.R.L.")
    test_db_session.add(cust); test_db_session.commit()
    today = date.today()
    a = Invoice(invoice_number="FT-A", amount=100.0, amount_due=100.0, issue_date=today - timedelta(days=60),
                due_date=today - timedelta(days=30), days_overdue=30, status="open", customer_id=cust.id,
                source_platform="fatturapro")
    b = Invoice(invoice_number="FT-B", amount=50.0, amount_due=50.0, issue_date=today - timedelta(days=40),
                due_date=today - timedelta(days=10), days_overdue=10, status="open", customer_id=cust.id,
                source_platform="fatturapro")
    test_db_session.add_all([a, b]); test_db_session.commit()
    ensure_open_case(test_db_session, cust); test_db_session.commit()
    return cust, a, b


def _post(client, cid, ids, channel):
    return client.post(f"/api/recovery/customers/{cid}/solleciti", json={"invoice_ids": ids, "channel": channel})


def test_email_and_phone_channels(test_client, test_db_session, cli):
    cust, a, b = cli
    r = _post(test_client, cust.id, [a.id], "email_copy")
    assert r.status_code == 200 and r.json()["registered"] is True
    act = test_db_session.query(RecoveryAction).get(r.json()["action_id"])
    assert act.channel == "email_copy" and "via Email" in act.notes
    r2 = _post(test_client, cust.id, [b.id], "phone")
    assert r2.status_code == 200
    assert test_db_session.query(RecoveryAction).get(r2.json()["action_id"]).channel == "phone"
    assert _post(test_client, cust.id, [a.id], "fax").status_code == 400


def test_last_click_wins_same_day(test_client, test_db_session, cli):
    cust, a, b = cli
    r1 = _post(test_client, cust.id, [a.id, b.id], "whatsapp_copy").json()
    r2 = _post(test_client, cust.id, [a.id, b.id], "email_copy").json()
    assert r2["already_registered_today"] is True and r2["action_id"] == r1["action_id"]
    act = test_db_session.query(RecoveryAction).get(r1["action_id"])
    assert act.channel == "email_copy" and "via Email" in act.notes
    done = test_db_session.query(RecoveryAction).filter_by(customer_id=cust.id).filter(
        RecoveryAction.completed_at.isnot(None)).count()
    assert done == 1  # un solo sollecito, non due


def test_invoice_note_row_and_dossier(test_client, test_db_session, cli):
    from backend.api.avvocato import _customer_dossier_files
    cust, a, b = cli
    r = test_client.put(f"/api/positions/{a.id}/note", json={"note": "Paga il 20 con bonifico"})
    assert r.status_code == 200 and r.json()["recovery_note"].startswith("Paga")
    det = test_client.get(f"/api/customers/{cust.id}").json()
    assert next(i for i in det["invoices"]["items"] if i["id"] == a.id)["recovery_note"] == "Paga il 20 con bonifico"
    files = _customer_dossier_files(test_db_session, cust)
    assert any(n.startswith("dossier_") and len(d) > 500 for n, d in files)
    assert test_client.put(f"/api/positions/{a.id}/note", json={"note": "  "}).json()["recovery_note"] is None


def test_history_per_invoice(test_client, test_db_session, cli):
    cust, a, b = cli
    _post(test_client, cust.id, [a.id, b.id], "whatsapp_copy")
    # secondo sollecito "ieri": simulo spostando la data del primo
    act = test_db_session.query(RecoveryAction).filter_by(customer_id=cust.id, action_type="first_contact").first()
    act.completed_at = datetime.utcnow() - timedelta(days=3); act.created_at = act.completed_at
    test_db_session.commit()
    _post(test_client, cust.id, [a.id], "email_copy")
    det = test_client.get(f"/api/customers/{cust.id}").json()
    by = {i["id"]: i for i in det["invoices"]["items"]}
    ha = by[a.id]["history"]
    assert [h["n"] for h in ha] == [1, 2] and ha[-1]["channel"] == "email_copy"
    assert by[a.id]["last_channel"] == "email_copy" and by[a.id]["last_action_at"] is not None
    assert [h["n"] for h in by[b.id]["history"]] == [1] and by[b.id]["last_channel"] == "whatsapp_copy"
    assert by[a.id]["sollecito_count"] == 2 and by[b.id]["sollecito_count"] == 1


# ── Trovati dalla review avversariale della Fase 5 ──────────────────────────

def test_history_note_does_not_hide_last_sollecito(test_client, test_db_session, cli):
    """Una nota di gruppo VECCHIA (completed_at NULL) non deve finire in coda
    alla storia né diventare l'"ultima azione" della riga."""
    from backend.engine.action_invoices import set_action_invoices
    cust, a, b = cli
    case = ensure_open_case(test_db_session, cust)
    note = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="note", notes="vecchia nota",
                          invoice_ids=[a.id], created_at=datetime.utcnow() - timedelta(days=10))
    test_db_session.add(note); test_db_session.commit()
    set_action_invoices(test_db_session, note.id, [a.id]); test_db_session.commit()
    r = _post(test_client, cust.id, [a.id], "whatsapp_copy").json()
    inv = next(i for i in test_client.get(f"/api/customers/{cust.id}").json()["invoices"]["items"] if i["id"] == a.id)
    assert [h["action_type"] for h in inv["history"]] == ["note", "first_contact"]
    assert inv["last_action_at"].startswith(date.today().isoformat())
    assert inv["last_channel"] == "whatsapp_copy"
    assert inv["history"][-1]["action_id"] == r["action_id"]


def test_history_ordinal_counts_inherited_and_legacy(test_client, test_db_session, cli):
    """L'ordinale in riga è lo STESSO del toast: contatti ereditati e storico
    pre-tabella (invoice_ids NULL, nessuna riga di join) si contano."""
    cust, a, b = cli
    case = ensure_open_case(test_db_session, cust)
    case.inherited_contacts = 1
    legacy = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="first_contact", channel="whatsapp_copy",
                            completed_at=datetime.utcnow() - timedelta(days=5), outcome="contacted", invoice_ids=None)
    test_db_session.add(legacy); test_db_session.commit()
    r = _post(test_client, cust.id, [a.id], "email_copy").json()
    inv = next(i for i in test_client.get(f"/api/customers/{cust.id}").json()["invoices"]["items"] if i["id"] == a.id)
    ordinals = [(h["n"], h["legacy"]) for h in inv["history"] if h["action_type"] != "note"]
    assert ordinals == [(2, True), (3, False)]  # ereditato 1 → legacy = n. 2 → oggi = n. 3
    assert r["sollecito_n"] == 3
    assert inv["history"][-1]["n"] == r["sollecito_n"]


def test_history_legacy_lawyer_delivery(test_client, test_db_session, cli):
    """Consegna al legale dello storico (invoice_ids NULL) → compare nella
    storia delle fatture già scadute a quella data, non delle altre."""
    cust, a, b = cli
    case = ensure_open_case(test_db_session, cust)
    when = datetime.utcnow() - timedelta(days=20)  # FT-A scaduta 30gg fa: sì; FT-B 10gg fa: no
    test_db_session.add(RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="lawyer",
                                       completed_at=when, invoice_ids=None))
    test_db_session.commit()
    items = {i["id"]: i for i in test_client.get(f"/api/customers/{cust.id}").json()["invoices"]["items"]}
    assert [h["action_type"] for h in items[a.id]["history"]] == ["lawyer"]
    assert items[a.id]["history"][0]["legacy"] is True
    assert items[b.id]["history"] == []


def test_last_click_keeps_manual_notes_and_audits(test_client, test_db_session, cli):
    """Il cambio canale non riscrive una nota scritta a mano e lascia traccia
    del canale precedente nell'audit."""
    from backend.database import ActivityLog
    cust, a, b = cli
    r1 = _post(test_client, cust.id, [a.id], "whatsapp_copy").json()
    act = test_db_session.query(RecoveryAction).get(r1["action_id"])
    act.notes = "Sollecito n. 1 - parlato via telefono (il titolare) - paga lunedì"
    test_db_session.commit()
    _post(test_client, cust.id, [a.id], "email_copy")
    test_db_session.refresh(act)
    assert act.channel == "email_copy"
    assert act.notes == "Sollecito n. 1 - parlato via telefono (il titolare) - paga lunedì"
    log = test_db_session.query(ActivityLog).filter_by(action="sollecito_channel_changed").one()
    assert log.details["from"] == "whatsapp_copy" and log.details["to"] == "email_copy"


def test_last_click_updates_every_todays_action(test_client, test_db_session, cli):
    """Due stadi nello stesso giorno = due azioni: il click successivo sulle
    stesse fatture aggiorna il canale di ENTRAMBE."""
    cust, a, b = cli
    case = ensure_open_case(test_db_session, cust)
    # FT-A ha già un sollecito ieri → oggi è al 2°; FT-B è al 1°
    from backend.engine.action_invoices import set_action_invoices
    prev = RecoveryAction(customer_id=cust.id, case_id=case.id, action_type="first_contact", channel="whatsapp_copy",
                          completed_at=datetime.utcnow() - timedelta(days=1), outcome="contacted", invoice_ids=[a.id])
    test_db_session.add(prev); test_db_session.commit()
    set_action_invoices(test_db_session, prev.id, [a.id]); test_db_session.commit()
    _post(test_client, cust.id, [a.id], "whatsapp_copy")
    _post(test_client, cust.id, [b.id], "whatsapp_copy")
    r = _post(test_client, cust.id, [a.id, b.id], "phone")
    assert r.status_code == 200 and r.json()["already_registered_today"] is True
    todays = test_db_session.query(RecoveryAction).filter(
        RecoveryAction.customer_id == cust.id, RecoveryAction.completed_at >= datetime.utcnow() - timedelta(hours=1)
    ).all()
    assert len(todays) == 2 and {x.channel for x in todays} == {"phone"}


def test_invoice_note_too_long_rejected(test_client, cli):
    cust, a, b = cli
    assert test_client.put(f"/api/positions/{a.id}/note", json={"note": "x" * 2001}).status_code == 400
    assert test_client.put(f"/api/positions/{a.id}/note", json={"note": "x" * 2000}).status_code == 200
