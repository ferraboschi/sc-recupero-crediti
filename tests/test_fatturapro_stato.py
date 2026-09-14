# -*- coding: utf-8 -*-
"""Stato SDI (regola owner): si importano SOLO le fatture consegnate / non
consegnate; bozze, in elaborazione e scartate restano fuori; una fattura
importata che si rivela bozza o sparisce prima della consegna viene ANNULLATA
(mai 'pagata'); verifica per cliente contro FatturaPro."""
from datetime import date, datetime, timedelta

import pytest

from backend.connectors.fatturapro import FatturaProConnector
from backend.database import Customer, Invoice, ActivityLog, RecoveryCase
from backend.engine.sdi import sdi_state_from_notifications, sdi_state_from_label
from backend.engine.fp_verify import compare_documents
from backend.engine.overdue import overdue_clause


# ── Parsing: firma di riga, colonna Stato, notifiche ─────────────────────

def _row(number, actions="", state=None, doc_id="900", saldo="100,00"):
    st = f"<td>{state}</td>" if state is not None else ""
    return (f'<tr><td>{number}</td><td>01/05/2026</td><td>ACME SRL</td><td>100,00</td><td>{saldo}</td>{st}'
            f'<td><a data-doc_id="{doc_id}" href="#">x</a>{actions}</td></tr>')


A_SEND = '<a data-action="invia_doc" data-primary="1" href="#">Invia</a>'
A_NOTIF = '<a data-action="show_notifiche" data-doc_id="1" href="#">Notifiche</a>'
A_OTHER = '<a data-action="registra_incasso" data-primary="1" href="#">Incasso</a>'
HEADER = "<tr><th>Documento</th><th>Data</th><th>Destinatario</th><th>Totale</th><th>Saldo</th></tr>"
HEADER_STATO = "<tr><th>Documento</th><th>Data</th><th>Destinatario</th><th>Totale</th><th>Saldo</th><th>Stato</th></tr>"


def test_row_signature_from_actions():
    conn = FatturaProConnector()
    html = "<table>" + HEADER + _row("A", A_SEND + A_OTHER) + _row("B", A_NOTIF + A_OTHER) + _row("C", A_OTHER) + _row("D", "") + "</table>"
    rows = conn._parse_invoice_table(html, conn._derive_column_map(html))
    sig = {r["invoice_number"]: r["fp_signature"] for r in rows}
    assert sig == {"A": "draft", "B": "notified", "C": "sent", "D": None}


def test_stato_column_parsed_from_full_list():
    conn = FatturaProConnector()
    html = "<table>" + HEADER_STATO + _row("A", A_SEND, state="Inviabile") + _row("B", A_NOTIF, state="Consegnato") + _row("C", A_OTHER, state="Inviato SDI") + "</table>"
    colmap = conn._derive_column_map(html)
    assert colmap["stato"] == 5
    rows = conn._parse_invoice_table(html, colmap)
    assert [r["fp_state_label"] for r in rows] == ["Inviabile", "Consegnato", "Inviato SDI"]
    assert [sdi_state_from_label(r["fp_state_label"]) for r in rows] == ["draft", "consegnata", "sent"]


def test_sdi_state_mappings():
    assert sdi_state_from_notifications(["RicevutaConsegna", "IT01_x_RC_002.xml"]) == "consegnata"
    assert sdi_state_from_notifications(["NotificaMancataConsegna"]) == "mancata_consegna"
    assert sdi_state_from_notifications(["RicevutaConsegna", "NotificaScarto"]) == "scartata"
    assert sdi_state_from_notifications([]) == "sent"
    assert sdi_state_from_label("Non consegnato") == "mancata_consegna"
    assert sdi_state_from_label("Scartato") == "scartata"
    assert sdi_state_from_label("Boh") is None


def test_fetch_sdi_notifications_parses_modal(monkeypatch):
    conn = FatturaProConnector()
    conn._authenticated = True
    conn._documenti_key = "k1"

    class R:
        status_code = 200
        url = "https://cloud.fatturapro.click/xcrud/xcrud_ajax.php"

        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None

    posted = {}

    def fake_post(url, data=None, **kw):
        posted.update(data)
        return R('<div class="modal"><ul><li><a href="displayMessaggioSDI.php?file=IT05_0q_RC_002.xml">RicevutaConsegna</a></li></ul>'
                 '<input type="hidden" name="key" value="k2"></div>')
    monkeypatch.setattr(conn.client, "post", fake_post)
    names = conn.fetch_sdi_notifications("4606197")
    assert names == ["RicevutaConsegna", "IT05_0q_RC_002.xml"]
    assert posted["xcrud[action]"] == "show_notifiche" and posted["xcrud[doc_id]"] == "4606197"
    assert conn._documenti_key == "k2"  # la chiave ruota a ogni risposta
    monkeypatch.setattr(conn.client, "post", lambda *a, **k: R('<div class="xcrud-error">x</div>'))
    assert conn.fetch_sdi_notifications("1") is None


# ── Sync: filtro per stato ────────────────────────────────────────────────

class FakeFP:
    raw = []
    notif = {}   # doc_id -> lista notifiche
    calls = []

    def __init__(self, *a, **k):
        pass

    def login(self):
        return True

    def fetch_overdue_invoices(self):
        return list(FakeFP.raw), False

    def fetch_scadenze_map(self, **kw):
        return {}, True

    def fetch_clienti_map(self):
        return {}, True

    def fetch_sdi_notifications(self, doc_id):
        FakeFP.calls.append(doc_id)
        return FakeFP.notif.get(str(doc_id))

    def close(self):
        pass


def _raw(number, doc_id, sig, balance=100.0, name="ACME SRL"):
    return {"invoice_number": number, "date": date(2026, 5, 1), "customer_name": name, "total": balance,
            "balance": balance, "doc_id": doc_id, "source_platform": "fatturapro", "fp_signature": sig}


def _sync(monkeypatch, session, raw, notif=None):
    from backend.api import sync as sync_mod
    FakeFP.raw = raw
    FakeFP.notif = notif or {}
    FakeFP.calls = []
    monkeypatch.setattr(sync_mod, "FatturaProConnector", FakeFP)
    monkeypatch.setattr(sync_mod, "get_session_direct", lambda: session)
    return sync_mod._sync_invoices_task()["fatturapro"]


def _get(session, number, **flt):
    """Il task di sync chiude la sessione: si rilegge per numero."""
    q = session.query(Invoice).filter_by(invoice_number=number)
    for k, v in flt.items():
        q = q.filter(getattr(Invoice, k) == v)
    return q.one()


def _mk(session, number, **kw):
    inv = Invoice(invoice_number=number, amount=kw.pop("amount", 100.0), amount_due=kw.pop("amount_due", 100.0),
                  issue_date=kw.pop("issue_date", date(2026, 4, 1)), due_date=kw.pop("due_date", date(2026, 5, 1)),
                  days_overdue=kw.pop("days_overdue", 30), source_platform="fatturapro", status=kw.pop("status", "open"), **kw)
    session.add(inv); session.commit()
    return inv


def test_new_rows_filtered_by_sdi_state(monkeypatch, test_db_session):
    r = _sync(monkeypatch, test_db_session, [
        _raw("D/2026", "1", "draft"),
        _raw("S/2026", "2", "sent"),
        _raw("RC/2026", "3", "notified"),
        _raw("NS/2026", "4", "notified"),
        _raw("MC/2026", "5", "notified"),
        _raw("LEG/2026", "6", None),
    ], notif={"3": ["RicevutaConsegna"], "4": ["NotificaScarto"], "5": ["NotificaMancataConsegna"]})
    nums = {i.invoice_number: i for i in test_db_session.query(Invoice).all()}
    assert set(nums) == {"RC/2026", "MC/2026", "LEG/2026"}
    assert nums["RC/2026"].sdi_state == "consegnata" and nums["MC/2026"].sdi_state == "mancata_consegna"
    assert nums["LEG/2026"].sdi_state is None  # layout senza azioni: compatibilità
    assert r["created"] == 3 and r["skipped_draft"] == 1 and r["skipped_pending"] == 1 and r["skipped_scartata"] == 1
    assert r["signature_unknown"] == 1
    # una sola chiamata notifiche per documento
    assert sorted(FakeFP.calls) == ["3", "4", "5"]


def test_existing_invoice_that_is_a_draft_gets_voided_not_counted(monkeypatch, test_db_session):
    _mk(test_db_session, "X/2026", source_id="10")
    r = _sync(monkeypatch, test_db_session, [_raw("X/2026", "10", "draft")])
    inv = _get(test_db_session, "X/2026")
    assert inv.status == "void" and inv.amount_due == 0 and inv.days_overdue == 0 and inv.sdi_state == "draft"
    assert inv.void_reason and r["voided"] == 1
    assert test_db_session.query(Invoice).filter(overdue_clause()).count() == 0
    assert test_db_session.query(ActivityLog).filter_by(action="invoice_voided").count() == 1
    # trasmessa e consegnata in seguito → torna un credito aperto
    r2 = _sync(monkeypatch, test_db_session, [_raw("X/2026", "10", "notified", balance=80.0)], notif={"10": ["RicevutaConsegna"]})
    inv = _get(test_db_session, "X/2026")
    assert inv.status == "open" and inv.amount_due == 80.0 and inv.sdi_state == "consegnata" and r2["reactivated"] == 1


def test_reassigned_number_voids_old_and_creates_new(monkeypatch, test_db_session):
    _mk(test_db_session, "N/2026", source_id="10", amount=459.42, amount_due=459.42)
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "77", "notified", balance=120.0)], notif={"77": ["RicevutaConsegna"]})
    old = _get(test_db_session, "N/2026", source_id="10")
    assert old.status == "void" and "riassegnato" in old.void_reason
    new = test_db_session.query(Invoice).filter(Invoice.invoice_number == "N/2026", Invoice.status == "open").one()
    assert new.source_id == "77" and new.amount_due == 120.0 and new.sdi_state == "consegnata"
    assert r["voided"] == 1 and r["created"] == 1


def test_absent_draft_is_voided_never_paid(monkeypatch, test_db_session):
    _mk(test_db_session, "GONE/2026", source_id="1", sdi_state="draft", missing_streak=1)
    _mk(test_db_session, "LEG/2026", source_id="2", missing_streak=1)
    r = _sync(monkeypatch, test_db_session, [_raw("OTHER/2026", "3", None)])
    d = _get(test_db_session, "GONE/2026"); legacy = _get(test_db_session, "LEG/2026")
    assert d.status == "void" and d.paid_at is None and "prima della consegna" in d.void_reason
    assert legacy.status == "paid"  # comportamento storico intatto per le righe non classificate
    assert r["voided"] == 1 and r["paid_detected"] == 1


def test_case_closes_when_only_void_remains(test_db_session):
    from backend.engine.cases import ensure_open_case, update_case_lifecycle
    cust = Customer(ragione_sociale="Void SRL"); test_db_session.add(cust); test_db_session.commit()
    inv = _mk(test_db_session, "V/2026", customer_id=cust.id)
    ensure_open_case(test_db_session, cust); test_db_session.commit()
    inv.status = "void"; inv.amount_due = 0; inv.days_overdue = 0; test_db_session.commit()
    stats = update_case_lifecycle(test_db_session)
    assert stats["closed"] == 1
    assert test_db_session.query(RecoveryCase).filter_by(customer_id=cust.id, status="open").count() == 0


# ── Verifica per cliente ──────────────────────────────────────────────────

def _fp(number, doc_id, total, balance, label):
    return {"invoice_number": number, "doc_id": doc_id, "total": total, "balance": balance,
            "date": date(2026, 6, 1), "customer_name": "CECCONI MARIO S.R.L.", "fp_state_label": label}


def test_compare_documents_verdicts(test_db_session):
    cust = Customer(ragione_sociale="CECCONI MARIO S.R.L."); test_db_session.add(cust); test_db_session.commit()
    ok = _mk(test_db_session, "1237", customer_id=cust.id, source_id="a", amount=888.96, amount_due=888.96)
    ghost = _mk(test_db_session, "1609", customer_id=cust.id, source_id="g")
    reass = _mk(test_db_session, "1500", customer_id=cust.id, source_id="old")
    paid_pl = _mk(test_db_session, "0966", customer_id=cust.id, source_id="p", status="paid", amount=859.08, amount_due=0)
    open_but_paid = _mk(test_db_session, "0824", customer_id=cust.id, source_id="q", amount=662.89)
    draft_pl = _mk(test_db_session, "1600", customer_id=cust.id, source_id="d")
    diff = _mk(test_db_session, "1286", customer_id=cust.id, source_id="c", amount=1000.0, amount_due=1000.0)
    voided = _mk(test_db_session, "1420", customer_id=cust.id, source_id="v", status="void", amount_due=0)
    res = compare_documents([ok, ghost, reass, paid_pl, open_but_paid, draft_pl, diff, voided], [
        _fp("1237", "a", 888.96, 888.96, "Consegnato"),
        _fp("1500", "new", 100.0, 100.0, "Consegnato"),
        _fp("0966", "p", 859.08, 0.0, "Consegnato"),
        _fp("0824", "q", 662.89, 0.0, "Consegnato"),
        _fp("1600", "d", 459.42, 459.42, "Inviabile"),
        _fp("1286", "c", 1400.45, 1400.45, "Consegnato"),
        _fp("1420", "v", 1044.66, 1044.66, "Consegnato"),
        _fp("1700", "m", 300.0, 300.0, "Consegnato"),
        _fp("0153", "z", 375.6, 0.0, "Consegnato"),
        _fp("1701", "b", 50.0, 50.0, "Inviato SDI"),
    ])
    v = {r["invoice_number"]: r["verdict"] for r in res["rows"]}
    assert v == {
        "1237": "ok", "1609": "inesistente", "1500": "numero_riassegnato", "0966": "ok",
        "0824": "pagata_su_fatturapro", "1600": "non_valida", "1286": "importo_diverso",
        "1420": "da_riattivare", "1700": "mancante", "0153": "pagata_non_tracciata",
    }
    assert "1701" not in v  # in elaborazione e non in piattaforma: nulla da segnalare
    assert res["summary"]["mancante"] == 1


class FakeSearchFP:
    rows = []
    searched = []

    def __init__(self, *a, **k):
        pass

    def login(self):
        return True

    def search_documents(self, phrase, limit=300):
        FakeSearchFP.searched.append(phrase)
        return list(FakeSearchFP.rows), True

    def close(self):
        pass


def test_verify_and_apply_endpoints(test_client, test_db_session, monkeypatch):
    import backend.connectors.fatturapro as fpmod
    monkeypatch.setattr(fpmod, "FatturaProConnector", FakeSearchFP)
    cust = Customer(ragione_sociale="Cecconi Mario Srl"); test_db_session.add(cust); test_db_session.commit()
    ghost = _mk(test_db_session, "1609", customer_id=cust.id, source_id="g", customer_name_raw="CECCONI MARIO S.R.L.")
    _mk(test_db_session, "1237", customer_id=cust.id, source_id="a", amount=888.96, amount_due=888.96, customer_name_raw="CECCONI MARIO S.R.L.")
    FakeSearchFP.rows = [_fp("1237", "a", 888.96, 888.96, "Consegnato"), _fp("1700", "m", 300.0, 300.0, "Consegnato"),
                         _fp("1600", "d", 459.42, 459.42, "Inviabile")]
    FakeSearchFP.searched = []
    r = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "CECCONI MARIO S.R.L." in body["searched_names"] and "Cecconi Mario Srl" in body["searched_names"]
    v = {x["invoice_number"]: x for x in body["rows"]}
    assert v["1609"]["verdict"] == "inesistente" and v["1609"]["fix"] == "void"
    assert v["1700"]["verdict"] == "mancante" and v["1700"]["fix"] == "import"
    assert "1600" not in v and v["1237"]["verdict"] == "ok"
    a = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro/apply", json={"fixes": [
        {"invoice_number": "1609", "fix": "void"}, {"invoice_number": "1700", "fix": "import"},
        {"invoice_number": "1237", "fix": "void"},  # verdetto ok: non si applica
    ]})
    assert a.status_code == 200, a.text
    assert {x["invoice_number"] for x in a.json()["applied"]} == {"1609", "1700"}
    assert a.json()["skipped"][0]["invoice_number"] == "1237"
    test_db_session.refresh(ghost)
    assert ghost.status == "void" and ghost.amount_due == 0
    imported = test_db_session.query(Invoice).filter_by(invoice_number="1700").one()
    assert imported.customer_id == cust.id and imported.sdi_state == "consegnata" and imported.amount_due == 300.0
    det = test_client.get(f"/api/customers/{cust.id}").json()
    assert [x["invoice_number"] for x in det["invoices"]["voided"]] == ["1609"]
    assert "1609" not in [x["invoice_number"] for x in det["invoices"]["items"]]


def test_verify_ignores_homonyms_and_duplicates_elsewhere(test_client, test_db_session, monkeypatch):
    import backend.connectors.fatturapro as fpmod
    monkeypatch.setattr(fpmod, "FatturaProConnector", FakeSearchFP)
    cust = Customer(ragione_sociale="ROSSI SRL"); test_db_session.add(cust); test_db_session.commit()
    other = Customer(ragione_sociale="ROSSI & C. SNC"); test_db_session.add(other); test_db_session.commit()
    _mk(test_db_session, "0500", customer_id=other.id, source_id="o", customer_name_raw="ROSSI & C. SNC")
    homonym = dict(_fp("0500", "o", 100.0, 100.0, "Consegnato")); homonym["customer_name"] = "ROSSI & C. SNC"
    mine = dict(_fp("0600", "m", 200.0, 200.0, "Consegnato")); mine["customer_name"] = "Rossi S.r.l."
    FakeSearchFP.rows = [homonym, mine]
    r = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    nums = {x["invoice_number"] for x in r["rows"]}
    assert nums == {"0600"} and r["fatturapro_documents"] == 1  # l'omonimo non entra
    # se un numero fosse già in piattaforma su un altro cliente, l'import non duplica
    _mk(test_db_session, "0600", customer_id=other.id, source_id="m", customer_name_raw="Rossi S.r.l.")
    a = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro/apply", json={"fixes": [{"invoice_number": "0600", "fix": "import"}]}).json()
    assert a["applied"] == [] and "già presente" in a["skipped"][0]["reason"]


# ── Trovati dalla review avversariale (frontend/contratto) ──────────────────

def test_sync_prefers_active_row_over_voided_duplicate(monkeypatch, test_db_session):
    """Stesso numero: riga annullata (documento vecchio) + riga attiva (nuovo).
    Il sync aggiorna l'ATTIVA e non crea un terzo duplicato."""
    _mk(test_db_session, "DUP/2026", source_id="old", status="void", amount_due=0, days_overdue=0, sdi_state="draft")
    _mk(test_db_session, "DUP/2026", source_id="new", amount_due=120.0, sdi_state="consegnata")
    r = _sync(monkeypatch, test_db_session, [_raw("DUP/2026", "new", "notified", balance=100.0)])
    rows = test_db_session.query(Invoice).filter_by(invoice_number="DUP/2026").order_by(Invoice.id).all()
    assert [x.status for x in rows] == ["void", "open"] and rows[1].amount_due == 100.0
    assert r["created"] == 0 and r["voided"] == 0


def test_compare_with_void_duplicate_and_incomplete_list(test_db_session):
    cust = Customer(ragione_sociale="Dup SRL"); test_db_session.add(cust); test_db_session.commit()
    old = _mk(test_db_session, "0700", customer_id=cust.id, source_id="old", status="void", amount_due=0)
    new = _mk(test_db_session, "0700", customer_id=cust.id, source_id="new", amount=50.0, amount_due=50.0)
    other = _mk(test_db_session, "0701", customer_id=cust.id, source_id="x")
    res = compare_documents([old, new, other], [_fp("0700", "new", 50.0, 50.0, "Consegnato")])
    v = {r["invoice_number"]: r for r in res["rows"]}
    assert v["0700"]["verdict"] == "ok" and v["0700"]["platform"]["id"] == new.id  # vince l'attiva
    assert v["0701"]["verdict"] == "inesistente" and v["0701"]["fix"] == "void"
    # lista incompleta: l'assenza non è una prova
    res2 = compare_documents([old, new, other], [_fp("0700", "new", 50.0, 50.0, "Consegnato")], complete=False)
    v2 = {r["invoice_number"]: r for r in res2["rows"]}
    assert v2["0701"]["verdict"] == "non_verificabile" and v2["0701"]["fix"] is None
    # solo annullata + documento valido con lo stesso doc_id → riattivabile
    res3 = compare_documents([old], [_fp("0700", "old", 50.0, 50.0, "Consegnato")])
    assert res3["rows"][0]["verdict"] == "da_riattivare" and res3["rows"][0]["fix_safe"] is False
    # numero riassegnato → replace (mai loop void/reactivate)
    res4 = compare_documents([new], [_fp("0700", "newer", 60.0, 60.0, "Consegnato")])
    assert res4["rows"][0]["verdict"] == "numero_riassegnato" and res4["rows"][0]["fix"] == "replace"


def test_apply_replace_and_incomplete_guard(test_client, test_db_session, monkeypatch):
    import backend.connectors.fatturapro as fpmod
    monkeypatch.setattr(fpmod, "FatturaProConnector", FakeSearchFP)
    cust = Customer(ragione_sociale="CECCONI MARIO S.R.L."); test_db_session.add(cust); test_db_session.commit()
    old = _mk(test_db_session, "1609", customer_id=cust.id, source_id="old", amount=459.42, amount_due=459.42,
              customer_name_raw="CECCONI MARIO S.R.L.")
    FakeSearchFP.rows = [_fp("1609", "newdoc", 300.0, 300.0, "Consegnato")]
    r = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    assert r["rows"][0]["verdict"] == "numero_riassegnato" and r["rows"][0]["fix"] == "replace"
    a = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro/apply", json={"fixes": [{"invoice_number": "1609", "fix": "replace"}]}).json()
    assert [x["fix"] for x in a["applied"]] == ["replace"]
    rows = test_db_session.query(Invoice).filter_by(invoice_number="1609").order_by(Invoice.id).all()
    assert [(x.status, x.source_id) for x in rows] == [("void", "old"), ("open", "newdoc")]
    assert rows[0].sdi_state is None  # niente stato del documento nuovo sulla riga vecchia
    # ricerca fallita → nessun annullamento per assenza
    class BrokenFP(FakeSearchFP):
        def search_documents(self, phrase, limit=300):
            return [], False
    monkeypatch.setattr(fpmod, "FatturaProConnector", BrokenFP)
    r2 = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    assert r2["complete"] is False
    assert all(x["verdict"] == "non_verificabile" and x["fix"] is None for x in r2["rows"])
    a2 = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro/apply", json={"fixes": [{"invoice_number": "1609", "fix": "void"}]}).json()
    assert a2["applied"] == [] and a2["skipped"]
