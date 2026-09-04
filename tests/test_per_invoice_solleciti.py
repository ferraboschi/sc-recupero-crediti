"""Solleciti PER-FATTURA — fondazione fattura-centrica (Fase 1).

Il soggetto del recupero è la FATTURA: ogni fattura ha il suo numero di
solleciti, indipendente dalle altre dello stesso cliente. Copre:
- la tabella di join (dual-write) e il conteggio deriva-on-read;
- l'esclusione di annullati / non-contatti dal conteggio;
- il dual-write dall'endpoint sollecito (nuovo + merge stesso giorno);
- il backfill dello storico (da invoice_ids e legacy due_date<data);
- l'esposizione del conteggio nel dettaglio cliente;
- il dossier avvocato che riporta lo stato per singola fattura.
"""
from datetime import date, datetime, timedelta

import pytest

from backend.database import (
    Customer, Invoice, RecoveryAction, RecoveryActionInvoice,
)
from backend.engine.cases import ensure_open_case
from backend.engine.action_invoices import (
    set_action_invoices,
    per_invoice_sollecito_stats,
    per_invoice_actions,
    backfill_action_invoices,
)


@pytest.fixture
def cust_two_invoices(test_db_session):
    """Cliente con due fatture scadute di età diversa (A più vecchia)."""
    cust = Customer(ragione_sociale="Ferro Distribuzione S.R.L.")
    test_db_session.add(cust)
    test_db_session.commit()
    today = date.today()
    inv_a = Invoice(
        invoice_number="FT-A", amount=800.0, amount_due=800.0,
        issue_date=today - timedelta(days=150), due_date=today - timedelta(days=120),
        days_overdue=120, status="open", customer_id=cust.id,
        source_platform="fatturapro",
    )
    inv_b = Invoice(
        invoice_number="FT-B", amount=300.0, amount_due=300.0,
        issue_date=today - timedelta(days=31), due_date=today - timedelta(days=1),
        days_overdue=1, status="open", customer_id=cust.id,
        source_platform="fatturapro",
    )
    test_db_session.add_all([inv_a, inv_b])
    test_db_session.commit()
    return cust, inv_a, inv_b


def _mk_action(session, cust, case, invoice_ids, when, cancelled=False,
               action_type="first_contact"):
    a = RecoveryAction(
        customer_id=cust.id, case_id=case.id, action_type=action_type,
        completed_at=when, outcome="contacted", channel="whatsapp_copy",
        invoice_ids=sorted(invoice_ids), cancelled=cancelled,
        created_at=when,
    )
    session.add(a)
    session.flush()
    set_action_invoices(session, a.id, invoice_ids)
    session.commit()
    return a


# ── Engine: conteggio per-fattura ───────────────────────────────────

def test_count_is_per_invoice(test_db_session, cust_two_invoices):
    cust, inv_a, inv_b = cust_two_invoices
    case = ensure_open_case(test_db_session, cust)
    d1 = datetime(2026, 6, 1)
    d2 = datetime(2026, 6, 20)
    _mk_action(test_db_session, cust, case, [inv_a.id], d1)
    _mk_action(test_db_session, cust, case, [inv_a.id, inv_b.id], d2,
               action_type="second_contact")

    stats = per_invoice_sollecito_stats(test_db_session, [inv_a.id, inv_b.id])
    assert stats[inv_a.id]["count"] == 2      # sollecitata due volte
    assert stats[inv_b.id]["count"] == 1      # una sola (la nuova)
    assert stats[inv_a.id]["last_at"] == d2   # ultima data
    assert stats[inv_b.id]["last_at"] == d2


def test_cancelled_and_non_contact_excluded(test_db_session, cust_two_invoices):
    cust, inv_a, inv_b = cust_two_invoices
    case = ensure_open_case(test_db_session, cust)
    _mk_action(test_db_session, cust, case, [inv_a.id], datetime(2026, 6, 1))
    # annullato → non conta
    _mk_action(test_db_session, cust, case, [inv_a.id], datetime(2026, 6, 2),
               cancelled=True, action_type="second_contact")
    # non-contatto (lawyer) → non conta nel numero, ma appare fra le azioni
    _mk_action(test_db_session, cust, case, [inv_a.id], datetime(2026, 6, 3),
               action_type="lawyer")

    stats = per_invoice_sollecito_stats(test_db_session, [inv_a.id])
    assert stats[inv_a.id]["count"] == 1

    acts = per_invoice_actions(test_db_session, [inv_a.id])
    # per_invoice_actions = STESSO predicato del conteggio: solo solleciti
    # (contatti completati, non annullati) → header e righe non divergono mai.
    types = {a.action_type for a in acts[inv_a.id]}
    assert types == {"first_contact"}  # niente lawyer, niente annullati


def test_stats_empty_for_uninvolved_invoice(test_db_session, cust_two_invoices):
    cust, inv_a, inv_b = cust_two_invoices
    case = ensure_open_case(test_db_session, cust)
    _mk_action(test_db_session, cust, case, [inv_a.id], datetime(2026, 6, 1))
    stats = per_invoice_sollecito_stats(test_db_session, [inv_a.id, inv_b.id])
    assert stats.get(inv_a.id, {}).get("count") == 1
    assert inv_b.id not in stats  # nessun sollecito → assente (get→0)


def test_set_action_invoices_idempotent(test_db_session, cust_two_invoices):
    cust, inv_a, inv_b = cust_two_invoices
    case = ensure_open_case(test_db_session, cust)
    a = _mk_action(test_db_session, cust, case, [inv_a.id], datetime(2026, 6, 1))
    # ri-applicare non duplica
    set_action_invoices(test_db_session, a.id, [inv_a.id, inv_a.id])
    test_db_session.commit()
    rows = test_db_session.query(RecoveryActionInvoice).filter_by(action_id=a.id).count()
    assert rows == 1


# ── API: dual-write dall'endpoint sollecito ─────────────────────────

def test_endpoint_writes_join_rows(test_client, test_db_session, cust_two_invoices):
    cust, inv_a, inv_b = cust_two_invoices
    res = test_client.post(
        f"/api/recovery/customers/{cust.id}/solleciti",
        json={"invoice_ids": [inv_a.id], "channel": "whatsapp_copy"},
    )
    assert res.status_code == 200 and res.json()["registered"] is True
    stats = per_invoice_sollecito_stats(test_db_session, [inv_a.id, inv_b.id])
    assert stats[inv_a.id]["count"] == 1
    assert inv_b.id not in stats


def test_empty_invoice_ids_attributes_all_overdue(test_client, test_db_session, cust_two_invoices):
    """MAJOR fix: un sollecito SENZA selezione (invoice_ids vuoto) 'sollecita
    l'intero debito' → attribuito a TUTTE le scadute (niente '0 solleciti' su
    una fattura davvero sollecitata)."""
    cust, inv_a, inv_b = cust_two_invoices
    res = test_client.post(
        f"/api/recovery/customers/{cust.id}/solleciti",
        json={"invoice_ids": [], "channel": "whatsapp_copy"},
    )
    assert res.status_code == 200 and res.json()["registered"] is True
    stats = per_invoice_sollecito_stats(test_db_session, [inv_a.id, inv_b.id])
    assert stats[inv_a.id]["count"] == 1
    assert stats[inv_b.id]["count"] == 1


def test_same_day_merge_writes_both_invoices(test_client, test_db_session, cust_two_invoices):
    cust, inv_a, inv_b = cust_two_invoices
    r1 = test_client.post(
        f"/api/recovery/customers/{cust.id}/solleciti",
        json={"invoice_ids": [inv_a.id], "channel": "whatsapp_copy"},
    ).json()
    r2 = test_client.post(
        f"/api/recovery/customers/{cust.id}/solleciti",
        json={"invoice_ids": [inv_b.id], "channel": "whatsapp_copy"},
    ).json()
    # stesso giorno → merge sulla stessa azione
    assert r2.get("already_registered_today") is True
    assert r1["action_id"] == r2["action_id"]
    stats = per_invoice_sollecito_stats(test_db_session, [inv_a.id, inv_b.id])
    # una sola azione (un sollecito) che ora cita ENTRAMBE
    assert stats[inv_a.id]["count"] == 1
    assert stats[inv_b.id]["count"] == 1


# ── Backfill dello storico ──────────────────────────────────────────

def test_backfill_from_invoice_ids(test_db_session, cust_two_invoices):
    cust, inv_a, inv_b = cust_two_invoices
    case = ensure_open_case(test_db_session, cust)
    # azione storica CON invoice_ids ma SENZA righe di join (pre-tabella)
    a = RecoveryAction(
        customer_id=cust.id, case_id=case.id, action_type="first_contact",
        completed_at=datetime(2026, 5, 1), invoice_ids=[inv_a.id, inv_b.id],
        created_at=datetime(2026, 5, 1),
    )
    test_db_session.add(a)
    test_db_session.commit()
    assert test_db_session.query(RecoveryActionInvoice).count() == 0

    stats = backfill_action_invoices(test_db_session)
    assert stats["from_invoice_ids"] == 2
    got = per_invoice_sollecito_stats(test_db_session, [inv_a.id, inv_b.id])
    assert got[inv_a.id]["count"] == 1 and got[inv_b.id]["count"] == 1


def test_backfill_legacy_null_uses_due_date_rule(test_db_session, cust_two_invoices):
    cust, inv_a, inv_b = cust_two_invoices
    case = ensure_open_case(test_db_session, cust)
    # scadenze note: A scaduta PRIMA del sollecito, B DOPO
    inv_a.due_date = date(2026, 6, 1)
    inv_b.due_date = date(2026, 8, 1)
    # azione legacy: contatto completato, NESSUN invoice_ids
    a = RecoveryAction(
        customer_id=cust.id, case_id=case.id, action_type="first_contact",
        completed_at=datetime(2026, 7, 1), invoice_ids=None,
        created_at=datetime(2026, 7, 1),
    )
    test_db_session.add(a)
    test_db_session.commit()

    stats = backfill_action_invoices(test_db_session)
    assert stats["from_legacy_overdue"] == 1  # solo A (scaduta prima)
    got = per_invoice_sollecito_stats(test_db_session, [inv_a.id, inv_b.id])
    assert got.get(inv_a.id, {}).get("count") == 1
    assert inv_b.id not in got  # la nuova NON eredita il sollecito


def test_backfill_marker_idempotent(test_db_session, cust_two_invoices):
    cust, inv_a, inv_b = cust_two_invoices
    case = ensure_open_case(test_db_session, cust)
    _mk_action(test_db_session, cust, case, [inv_a.id], datetime(2026, 6, 1))
    first = backfill_action_invoices(test_db_session)
    assert "from_invoice_ids" in first
    second = backfill_action_invoices(test_db_session)
    assert second == {"skipped": True}


# ── Esposizione nel dettaglio cliente ───────────────────────────────

def test_customer_detail_exposes_sollecito_count(test_client, test_db_session, cust_two_invoices):
    cust, inv_a, inv_b = cust_two_invoices
    test_client.post(
        f"/api/recovery/customers/{cust.id}/solleciti",
        json={"invoice_ids": [inv_a.id], "channel": "whatsapp_copy"},
    )
    detail = test_client.get(f"/api/customers/{cust.id}").json()
    by_id = {i["id"]: i for i in detail["invoices"]["items"]}
    assert by_id[inv_a.id]["sollecito_count"] == 1
    assert by_id[inv_a.id]["last_sollecito"] is not None
    assert by_id[inv_b.id]["sollecito_count"] == 0
    assert by_id[inv_b.id]["last_sollecito"] is None


# ── Dossier avvocato: stato per fattura (note che viaggiano) ─────────

def test_customer_detail_degrades_without_join_table(
    test_client, test_db_session, test_db_engine, cust_two_invoices
):
    """MINOR 1: se la tabella di join non esiste ancora (prod indietro di una
    migration nella finestra iniziale del boot) la pagina cliente NON va in 500
    — mostra 0 solleciti e prosegue."""
    cust, inv_a, inv_b = cust_two_invoices
    RecoveryActionInvoice.__table__.drop(bind=test_db_engine)
    res = test_client.get(f"/api/customers/{cust.id}")
    assert res.status_code == 200
    items = res.json()["invoices"]["items"]
    assert all(i["sollecito_count"] == 0 for i in items)


def test_dossier_includes_per_invoice(test_db_session, cust_two_invoices):
    from backend.api.avvocato import _customer_dossier_files
    cust, inv_a, inv_b = cust_two_invoices
    case = ensure_open_case(test_db_session, cust)
    a = _mk_action(test_db_session, cust, case, [inv_a.id], datetime(2026, 6, 1))
    a.notes = "Cliente promette pagamento entro fine mese"
    test_db_session.commit()

    files = _customer_dossier_files(test_db_session, cust)
    names = [n for n, _ in files]
    # il dossier PDF esiste e non è vuoto (smoke: build non crasha col
    # nuovo blocco per-fattura)
    dossier = next(d for n, d in files if n.startswith("dossier_"))
    assert isinstance(dossier, (bytes, bytearray)) and len(dossier) > 500
    assert any(n.startswith("fatture/") for n in names)
