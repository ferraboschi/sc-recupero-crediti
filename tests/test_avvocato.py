"""Test della sezione Avvocato: candidati, dossier ZIP, handover."""
import io
import zipfile
from datetime import date, datetime, time, timedelta

from backend.database import Customer, Invoice, RecoveryCase, RecoveryAction


def _cust(s, nome, **kw):
    c = Customer(
        ragione_sociale=nome,
        partita_iva=kw.pop("partita_iva", None),
        excluded=kw.pop("excluded", False),
        recovery_status=kw.pop("recovery_status", "second_contact"),
        source=kw.pop("source", "shopify"),
        **kw,
    )
    s.add(c)
    s.commit()
    return c


def _inv(s, cust, num, amount, days_overdue=30, status="open"):
    i = Invoice(
        invoice_number=num,
        amount=amount,
        amount_due=amount,
        issue_date=date.today() - timedelta(days=days_overdue + 30),
        due_date=date.today() - timedelta(days=days_overdue),
        days_overdue=days_overdue,
        customer_id=cust.id,
        source_platform="fatturapro",
        status=status,
    )
    s.add(i)
    s.commit()
    return i


def _contact(s, cust, case, atype, days_ago=0):
    """Contatto completato che CITA le scadute del cliente (modello
    fattura-centrico: un sollecito cita sempre le fatture; lo storico legacy
    è collegato dal backfill)."""
    from backend.engine.action_invoices import set_action_invoices
    when = datetime.combine(date.today() - timedelta(days=days_ago), time(12, 0))
    ids = [i.id for i in s.query(Invoice).filter(
        Invoice.customer_id == cust.id, Invoice.status != "paid").all()]
    a = RecoveryAction(
        customer_id=cust.id, case_id=case.id, action_type=atype,
        completed_at=when, created_at=when, channel="whatsapp_copy",
        invoice_ids=ids,
    )
    s.add(a)
    s.flush()
    set_action_invoices(s, a.id, ids)
    s.commit()
    return a


def _make_candidate(s, nome="ACME SRL", debt=2000.0, solleciti=2, last_days_ago=20):
    cust = _cust(s, nome, partita_iva="12345670785")
    _inv(s, cust, f"{nome}-1", debt)
    case = RecoveryCase(customer_id=cust.id, status="open")
    s.add(case)
    s.commit()
    if solleciti >= 1:
        _contact(s, cust, case, "first_contact", days_ago=last_days_ago + 7)
    if solleciti >= 2:
        _contact(s, cust, case, "second_contact", days_ago=last_days_ago)
    return cust


# ── candidati ───────────────────────────────────────────────────────

def test_candidate_listed(test_client, test_db_session):
    _make_candidate(test_db_session, "ACME SRL", debt=2000, solleciti=2, last_days_ago=20)
    d = test_client.get("/api/avvocato/candidates").json()
    assert d["count"] == 1
    it = d["items"][0]
    assert it["ragione_sociale"] == "ACME SRL"
    assert it["contact_count"] == 2
    assert it["days_since_last_sollecito"] == 20
    assert it["ready"] is True  # 20 >= 14
    assert it["total_overdue"] == 2000.0


def test_below_threshold_excluded(test_client, test_db_session):
    _make_candidate(test_db_session, "Piccolo", debt=1000, solleciti=2)
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 0


def test_at_threshold_excluded(test_client, test_db_session):
    # 1500 esatti NON basta: serve > 1500.
    _make_candidate(test_db_session, "Soglia", debt=1500, solleciti=2)
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 0


def test_one_sollecito_excluded(test_client, test_db_session):
    _make_candidate(test_db_session, "UnoSolo", debt=2000, solleciti=1)
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 0


def test_recent_sollecito_flagged_not_ready(test_client, test_db_session):
    _make_candidate(test_db_session, "Recente", debt=2000, solleciti=2, last_days_ago=3)
    it = test_client.get("/api/avvocato/candidates").json()["items"][0]
    assert it["days_since_last_sollecito"] == 3
    assert it["ready"] is False  # < 14 (soglia grazia)


def test_excluded_customer_not_candidate(test_client, test_db_session):
    cust = _make_candidate(test_db_session, "Escluso", debt=2000, solleciti=2)
    cust.excluded = True
    test_db_session.commit()
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 0


def test_lawyer_stage_still_candidate(test_client, test_db_session):
    # recovery_status='lawyer' è solo lo STADIO (impostato dopo il 2° sollecito):
    # finché non c'è un handover (azione lawyer COMPLETATA) resta candidato.
    cust = _make_candidate(test_db_session, "StadioLegale", debt=2000, solleciti=2)
    cust.recovery_status = "lawyer"
    test_db_session.commit()
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 1


def test_pending_lawyer_todo_still_candidate(test_client, test_db_session):
    # Il todo legale PIANIFICATO dopo il 2° sollecito (completed_at=None) NON
    # è una consegna → il cliente deve restare candidato.
    cust = _make_candidate(test_db_session, "TodoLegale", debt=2000, solleciti=2)
    case = test_db_session.query(RecoveryCase).filter_by(customer_id=cust.id).first()
    test_db_session.add(RecoveryAction(
        customer_id=cust.id, case_id=case.id, action_type="lawyer",
        scheduled_date=date.today() + timedelta(days=14), completed_at=None))
    test_db_session.commit()
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 1


def test_delivered_completed_lawyer_action_not_candidate(test_client, test_db_session):
    cust = _make_candidate(test_db_session, "Consegnato", debt=2000, solleciti=2)
    case = test_db_session.query(RecoveryCase).filter_by(customer_id=cust.id).first()
    test_db_session.add(RecoveryAction(
        customer_id=cust.id, case_id=case.id, action_type="lawyer",
        completed_at=datetime.utcnow(), outcome="handover"))
    test_db_session.commit()
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 0


def test_merged_customer_not_candidate(test_client, test_db_session):
    cust = _make_candidate(test_db_session, "Fuso", debt=2000, solleciti=2)
    cust.merged_into = 9999
    test_db_session.commit()
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 0


# ── handover ────────────────────────────────────────────────────────

def test_handover_removes_from_list(test_client, test_db_session):
    cust = _make_candidate(test_db_session, "DaConsegnare", debt=2000, solleciti=2)
    r = test_client.post(f"/api/avvocato/customers/{cust.id}/handover")
    assert r.status_code == 200
    assert r.json()["recovery_status"] == "lawyer"
    test_db_session.refresh(cust)
    assert cust.recovery_status == "lawyer"
    # registrata l'azione datata
    act = test_db_session.query(RecoveryAction).filter(
        RecoveryAction.customer_id == cust.id,
        RecoveryAction.action_type == "lawyer",
    ).first()
    assert act is not None and act.completed_at is not None
    # esce dai candidati
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 0


def test_handover_idempotent(test_client, test_db_session):
    cust = _make_candidate(test_db_session, "Due volte", debt=2000, solleciti=2)
    test_client.post(f"/api/avvocato/customers/{cust.id}/handover")
    r2 = test_client.post(f"/api/avvocato/customers/{cust.id}/handover")
    assert r2.status_code == 200
    assert r2.json()["already"] is True
    lawyer_actions = test_db_session.query(RecoveryAction).filter(
        RecoveryAction.customer_id == cust.id,
        RecoveryAction.action_type == "lawyer",
    ).count()
    assert lawyer_actions == 1  # non duplica


# ── dossier ─────────────────────────────────────────────────────────

def test_dossier_zip_contains_dossier_and_invoices(test_client, test_db_session):
    cust = _make_candidate(test_db_session, "Dossier SRL", debt=2000, solleciti=2)
    r = test_client.get(f"/api/avvocato/customers/{cust.id}/dossier-zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert any(n.startswith("dossier_") and n.endswith(".pdf") for n in names)
    assert any("fattura" in n and n.endswith(".pdf") for n in names)


def test_dossier_handles_non_latin1_name(test_client, test_db_session):
    # Nome con caratteri fuori latin-1 (ideogrammi) → il PDF/ZIP non crasha.
    cust = _make_candidate(test_db_session, "Sushi 寿司 SRL", debt=2000, solleciti=2)
    r = test_client.get(f"/api/avvocato/customers/{cust.id}/dossier-zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"


def test_dossier_zip_all(test_client, test_db_session):
    a = _make_candidate(test_db_session, "Uno SRL", debt=2000, solleciti=2)
    b = _make_candidate(test_db_session, "Due SRL", debt=3000, solleciti=2)
    r = test_client.get("/api/avvocato/dossier-zip-all")
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    # una cartella per candidato, prefissata dall'id (niente collisioni di nome)
    assert any(n.startswith(f"{a.id}_Uno_SRL/") for n in names)
    assert any(n.startswith(f"{b.id}_Due_SRL/") for n in names)


def test_dossier_zip_all_name_collision_no_overwrite(test_client, test_db_session):
    # Due ragioni sociali diverse che collassano sullo stesso _safe non devono
    # sovrascriversi: l'id nel folder le tiene separate.
    a = _make_candidate(test_db_session, "Rossi & Figli", debt=2000, solleciti=2)
    b = _make_candidate(test_db_session, "Rossi, Figli", debt=2000, solleciti=2)
    r = test_client.get("/api/avvocato/dossier-zip-all")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    folders = {n.split("/")[0] for n in zf.namelist()}
    assert f"{a.id}_Rossi_Figli" in folders
    assert f"{b.id}_Rossi_Figli" in folders


# ── fix del controagente ────────────────────────────────────────────

def test_disputed_excluded_from_debt(test_client, test_db_session):
    # Contestata esclusa dal totale: 1000 open + 700 disputed → 1000 < 1500.
    cust = _cust(test_db_session, "Contestata SRL")
    _inv(test_db_session, cust, "OPEN-1", 1000.0)
    _inv(test_db_session, cust, "DISP-1", 700.0, status="disputed")
    case = RecoveryCase(customer_id=cust.id, status="open")
    test_db_session.add(case)
    test_db_session.commit()
    _contact(test_db_session, cust, case, "first_contact", days_ago=27)
    _contact(test_db_session, cust, case, "second_contact", days_ago=20)
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 0


def test_disputed_not_in_reported_total(test_client, test_db_session):
    cust = _cust(test_db_session, "Misto SRL")
    _inv(test_db_session, cust, "OPEN", 2000.0)
    _inv(test_db_session, cust, "DISP", 500.0, status="disputed")
    case = RecoveryCase(customer_id=cust.id, status="open")
    test_db_session.add(case)
    test_db_session.commit()
    _contact(test_db_session, cust, case, "first_contact", days_ago=27)
    _contact(test_db_session, cust, case, "second_contact", days_ago=20)
    it = test_client.get("/api/avvocato/candidates").json()["items"][0]
    assert it["total_overdue"] == 2000.0
    assert it["overdue_count"] == 1  # la contestata non conta


def test_inherited_only_not_candidate(test_client, test_db_session):
    # Pratica con contatti EREDITATI ma nessun sollecito FRESCO → non candidato
    # (evita il falso "pronto" di un caso riaperto mai risollecitato).
    cust = _cust(test_db_session, "Riaperto SRL")
    _inv(test_db_session, cust, "R-1", 2000.0)
    case = RecoveryCase(customer_id=cust.id, status="open", inherited_contacts=2)
    test_db_session.add(case)
    test_db_session.commit()
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 0


def test_excluded_null_still_candidate(test_client, test_db_session):
    cust = _make_candidate(test_db_session, "NullExcl SRL", debt=2000, solleciti=2)
    cust.excluded = None  # legacy row
    test_db_session.commit()
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 1


def test_past_delivered_reappears_new_cycle(test_client, test_db_session):
    # Consegnato in un ciclo PASSATO (chiuso) + NUOVO debito con 2 solleciti
    # freschi → deve RIAPPARIRE (delivered è scoped alla pratica aperta).
    cust = _cust(test_db_session, "Ritorno SRL")
    old = RecoveryCase(customer_id=cust.id, status="closed")
    test_db_session.add(old)
    test_db_session.commit()
    test_db_session.add(RecoveryAction(
        customer_id=cust.id, case_id=old.id, action_type="lawyer",
        completed_at=datetime.utcnow() - timedelta(days=200)))
    _inv(test_db_session, cust, "NEW-1", 2000.0)
    new = RecoveryCase(customer_id=cust.id, status="open")
    test_db_session.add(new)
    test_db_session.commit()
    _contact(test_db_session, cust, new, "first_contact", days_ago=27)
    _contact(test_db_session, cust, new, "second_contact", days_ago=20)
    assert test_client.get("/api/avvocato/candidates").json()["count"] == 1


def test_handover_no_open_case_rejected(test_client, test_db_session):
    # Nessuna pratica aperta (saldato) → 409, e NON si fabbrica una pratica.
    cust = _cust(test_db_session, "Saldato SRL")
    _inv(test_db_session, cust, "P", 100.0, status="paid")
    r = test_client.post(f"/api/avvocato/customers/{cust.id}/handover")
    assert r.status_code == 409
    assert test_db_session.query(RecoveryCase).filter_by(customer_id=cust.id).count() == 0
