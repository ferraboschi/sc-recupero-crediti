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

    existing_numbers = set()   # numeri che FatturaPro "conosce ancora" (ricerca per numero)
    search_ok = True

    def search_documents(self, phrase, limit=300, column="documenti.Destinatario"):
        FakeFP.calls.append(("search", column, phrase))
        hits = [{"invoice_number": n, "doc_id": "x", "total": 0.0, "balance": 0.0} for n in FakeFP.existing_numbers if phrase in n]
        return hits, FakeFP.search_ok

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
    FakeFP.existing_numbers = getattr(FakeFP, "_next_existing", set())
    FakeFP.search_ok = getattr(FakeFP, "_next_search_ok", True)
    FakeFP._next_existing = set(); FakeFP._next_search_ok = True
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
    """Riga della piattaforma. Di default col doc_id VERIFICATO (regime
    'identità = documento'); passare doc_id_verified=False per una riga storica
    (doc_id fossile: vale il numero)."""
    inv = Invoice(invoice_number=number, amount=kw.pop("amount", 100.0), amount_due=kw.pop("amount_due", 100.0),
                  issue_date=kw.pop("issue_date", date(2026, 4, 1)), due_date=kw.pop("due_date", date(2026, 5, 1)),
                  days_overdue=kw.pop("days_overdue", 30), source_platform="fatturapro", status=kw.pop("status", "open"),
                  doc_id_verified=kw.pop("doc_id_verified", True), **kw)
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


def test_reassigned_number_without_evidence_goes_to_payment_detection(monkeypatch, test_db_session):
    """Numero passato a un documento nuovo, documento vecchio NON più in lista e
    senza evidenza di bozza: la riga vecchia NON si annulla (poteva essere
    incassata) → payment detection per identità (doc_id); il nuovo entra."""
    _mk(test_db_session, "N/2026", source_id="10", amount=459.42, amount_due=459.42, sdi_state="consegnata")
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "77", "notified", balance=120.0)], notif={"77": ["RicevutaConsegna"]})
    old = _get(test_db_session, "N/2026", source_id="10")
    assert old.status == "open" and old.missing_streak == 1 and r["voided"] == 0 and r["created"] == 1
    new = _get(test_db_session, "N/2026", source_id="77")
    assert new.amount_due == 120.0 and new.sdi_state == "consegnata"
    # seconda assenza (per doc_id, anche se il NUMERO è ancora in lista) → pagata
    _sync(monkeypatch, test_db_session, [_raw("N/2026", "77", "notified", balance=120.0)])
    old = _get(test_db_session, "N/2026", source_id="10")
    assert old.status == "paid" and old.amount_due_at_paid == 459.42


def test_reassigned_number_with_draft_evidence_is_voided(monkeypatch, test_db_session):
    _mk(test_db_session, "D/2026", source_id="10", sdi_state="draft")
    r = _sync(monkeypatch, test_db_session, [_raw("D/2026", "77", "notified", balance=120.0)], notif={"77": ["RicevutaConsegna"]})
    old = _get(test_db_session, "D/2026", source_id="10")
    assert old.status == "void" and "riassegnato" in old.void_reason and r["voided"] == 1
    assert _get(test_db_session, "D/2026", source_id="77").status == "open"


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

    by_number = {}   # ripiego per numero: frase → righe

    def search_documents(self, phrase, limit=300, column="documenti.Destinatario"):
        FakeSearchFP.searched.append((column, phrase))
        if column != "documenti.Destinatario":
            return list(FakeSearchFP.by_number.get(phrase, [])), True
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
    FakeSearchFP.by_number = {}
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
    mine = dict(_fp("0600", "m", 200.0, 200.0, "Consegnato")); mine["customer_name"] = "rossi  srl"  # solo maiuscole/spazi diversi
    spa = dict(_fp("0650", "s", 100.0, 100.0, "Consegnato")); spa["customer_name"] = "ROSSI S.P.A."  # quasi-omonimo: fuori
    FakeSearchFP.rows = [homonym, mine, spa]
    r = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    nums = {x["invoice_number"] for x in r["rows"]}
    assert nums == {"0600"} and r["fatturapro_documents"] == 1  # omonimi e quasi-omonimi non entrano
    # se il documento è già posseduto (verificato) da un altro cliente, la verifica
    # non lo propone (foreign_docs) e l'apply non lo importa
    _mk(test_db_session, "0600", customer_id=other.id, source_id="m", customer_name_raw="Rossi S.r.l.")
    r2 = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    assert r2["foreign_docs"] == 1 and r2["rows"] == []
    a = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro/apply", json={"fixes": [{"invoice_number": "0600", "fix": "import"}]}).json()
    assert a["applied"] == [] and a["skipped"]


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
        def search_documents(self, phrase, limit=300, column="documenti.Destinatario"):
            return [], False
    monkeypatch.setattr(fpmod, "FatturaProConnector", BrokenFP)
    r2 = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    assert r2["complete"] is False
    assert all(x["verdict"] == "non_verificabile" and x["fix"] is None for x in r2["rows"])
    a2 = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro/apply", json={"fixes": [{"invoice_number": "1609", "fix": "void"}]}).json()
    assert a2["applied"] == [] and a2["skipped"]


# ── Trovati dalla review avversariale (backend) ─────────────────────────────

def test_last_notification_wins_and_scartata_is_rechecked(monkeypatch, test_db_session):
    assert sdi_state_from_notifications(["NotificaScarto", "RicevutaConsegna"]) == "consegnata"
    assert sdi_state_from_notifications(["RicevutaConsegna", "NotificaScarto"]) == "scartata"
    _mk(test_db_session, "RS/2026", source_id="9", sdi_state="scartata", status="void", amount_due=0, days_overdue=0)
    r = _sync(monkeypatch, test_db_session, [_raw("RS/2026", "9", "notified", balance=70.0)], notif={"9": ["NotificaScarto", "RicevutaConsegna"]})
    inv = _get(test_db_session, "RS/2026")
    assert inv.status == "open" and inv.sdi_state == "consegnata" and inv.amount_due == 70.0 and r["reactivated"] == 1


def test_absent_sent_is_paid_not_voided(monkeypatch, test_db_session):
    """'sent' = assenza di notifiche, non prova di mancata consegna: una
    fattura sparita in quello stato segue la regola storica (pagata)."""
    _mk(test_db_session, "SENT/2026", source_id="1", sdi_state="sent", missing_streak=1)
    r = _sync(monkeypatch, test_db_session, [_raw("OTHER/2026", "3", None)])
    inv = _get(test_db_session, "SENT/2026")
    assert inv.status == "paid" and r["paid_detected"] == 1 and r["voided"] == 0


def test_empty_notifications_do_not_create_evidence(monkeypatch, test_db_session):
    """Modale senza link ai messaggi SDI → [] → 'sent' (mai 'consegnata' né 'scartata')."""
    conn = FatturaProConnector(); conn._authenticated = True; conn._documenti_key = "k"

    class R:
        url = "x"

        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None
    monkeypatch.setattr(conn.client, "post", lambda *a, **k: R('<ul><li>Documenti</li><li>Esito positivo</li></ul>'))
    assert conn.fetch_sdi_notifications("1") == []
    assert sdi_state_from_notifications([]) == "sent"


def test_reassigned_number_never_voids_a_paid_row(monkeypatch, test_db_session):
    _mk(test_db_session, "P/2026", source_id="old", status="paid", amount_due=0, days_overdue=0, paid_at=datetime(2026, 8, 1), amount_due_at_paid=100.0)
    r = _sync(monkeypatch, test_db_session, [_raw("P/2026", "new", "notified", balance=55.0)], notif={"new": ["RicevutaConsegna"]})
    rows = test_db_session.query(Invoice).filter_by(invoice_number="P/2026").order_by(Invoice.id).all()
    assert [(x.status, x.source_id) for x in rows] == [("paid", "old"), ("open", "new")]
    assert r["voided"] == 0 and r.get("reassigned_on_paid") == 1
    assert test_db_session.query(ActivityLog).filter_by(action="numero_riassegnato_su_pagata").count() == 1


def test_draft_guard_blocks_mass_void(monkeypatch, test_db_session):
    """Se (quasi) tutta la lista è letta come bozza è cambiato il markup: in quel
    ciclo la firma 'draft' non vale, nessun annullamento."""
    for i in range(70):
        _mk(test_db_session, f"G{i}/2026", source_id=str(i))
    raw = [_raw(f"G{i}/2026", str(i), "draft") for i in range(70)]
    r = _sync(monkeypatch, test_db_session, raw)
    assert r["voided"] == 0 and r.get("draft_guard_triggered") == 70
    assert test_db_session.query(Invoice).filter_by(status="void").count() == 0


def test_verify_keeps_rows_known_by_doc_id_when_customer_renamed(test_client, test_db_session, monkeypatch):
    """Cliente rinominato in anagrafica FatturaPro: le sue fatture (doc_id
    noti) NON diventano 'inesistenti'."""
    import backend.connectors.fatturapro as fpmod
    monkeypatch.setattr(fpmod, "FatturaProConnector", FakeSearchFP)
    cust = Customer(ragione_sociale="VECCHIO NOME SRL"); test_db_session.add(cust); test_db_session.commit()
    _mk(test_db_session, "0900", customer_id=cust.id, source_id="d900", amount=100.0, customer_name_raw="VECCHIO NOME SRL")
    renamed = dict(_fp("0900", "d900", 100.0, 100.0, "Consegnato")); renamed["customer_name"] = "VECCHIO NOME SRL IN LIQUIDAZIONE"
    FakeSearchFP.rows = [renamed]
    r = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    assert r["rows"][0]["verdict"] == "ok"


def test_compare_flags_duplicate_active_rows(test_db_session):
    cust = Customer(ragione_sociale="Dup2 SRL"); test_db_session.add(cust); test_db_session.commit()
    a = _mk(test_db_session, "0800", customer_id=cust.id, source_id="a")
    b = _mk(test_db_session, "0800", customer_id=cust.id, source_id="b")
    res = compare_documents([a, b], [_fp("0800", "b", 100.0, 100.0, "Consegnato")])
    v = {(r["verdict"], r["platform"]["id"]) for r in res["rows"]}
    assert v == {("ok", b.id), ("duplicato", a.id)}
    assert not any(r["fix_safe"] for r in res["rows"] if r["verdict"] == "duplicato")


def test_search_documents_paginates(monkeypatch):
    conn = FatturaProConnector(); conn._authenticated = True
    header = HEADER_STATO
    page1 = "<table>" + header + "".join(_row(f"P{i}", A_NOTIF, state="Consegnato", doc_id=str(i)) for i in range(3)) + "</table><input type='hidden' name='key' value='k2'>"
    page2 = "<table>" + header + _row("P3", A_NOTIF, state="Consegnato", doc_id="3") + "</table>"

    class R:
        url = "x"

        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            return None
    calls = []
    monkeypatch.setattr(conn.client, "get", lambda *a, **k: R("<table>" + header + "</table><input type='hidden' name='key' value='k1'><input type='hidden' name='instance' value='documenti'>"))

    def fake_post(url, data=None, **kw):
        calls.append(dict(data))
        return R(page1 if data["xcrud[start]"] == "0" else page2)
    monkeypatch.setattr(conn.client, "post", fake_post)
    rows, complete = conn.search_documents("ACME", limit=3)
    assert [r["invoice_number"] for r in rows] == ["P0", "P1", "P2", "P3"] and complete is True
    assert [c["xcrud[start]"] for c in calls] == ["0", "3"] and calls[1]["xcrud[key]"] == "k2"
    assert all(c["xcrud[search]"] == "1" and c["xcrud[phrase]"] == "ACME" for c in calls)


# ── Trovati dalla review di secondo giro ────────────────────────────────────

def test_renamed_customer_falls_back_to_number_search(test_client, test_db_session, monkeypatch):
    """Ricerca per nome vuota (cliente rinominato su FatturaPro): ripiego per
    numero; se neppure così si trova nulla, l'assenza non è una prova."""
    import backend.connectors.fatturapro as fpmod
    monkeypatch.setattr(fpmod, "FatturaProConnector", FakeSearchFP)
    cust = Customer(ragione_sociale="VECCHIO NOME SRL"); test_db_session.add(cust); test_db_session.commit()
    _mk(test_db_session, "2026/00000900/SAK - Fattura", customer_id=cust.id, source_id="d900", amount=100.0, customer_name_raw="VECCHIO NOME SRL")
    _mk(test_db_session, "2026/00000901/SAK - Fattura", customer_id=cust.id, source_id="d901", amount=50.0, customer_name_raw="VECCHIO NOME SRL")
    FakeSearchFP.rows = []
    row900 = dict(_fp("2026/00000900/SAK - Fattura", "d900", 100.0, 100.0, "Consegnato")); row900["customer_name"] = "NUOVO NOME SPA"
    FakeSearchFP.by_number = {"00000900": [row900], "00000901": []}
    FakeSearchFP.searched = []
    r = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    v = {x["invoice_number"]: x["verdict"] for x in r["rows"]}
    assert v["2026/00000900/SAK - Fattura"] == "ok" and v["2026/00000901/SAK - Fattura"] == "inesistente"
    assert ("documenti.NumeroSezionale", "00000900") in FakeSearchFP.searched
    # nulla neppure per numero → lista incompleta, niente 'inesistente'
    FakeSearchFP.by_number = {}
    r2 = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    assert r2["complete"] is False and all(x["verdict"] == "non_verificabile" for x in r2["rows"])
    FakeSearchFP.by_number = {}


def test_draft_signature_never_voids_delivered_or_paid(monkeypatch, test_db_session):
    _mk(test_db_session, "DEL/2026", source_id="1", sdi_state="consegnata")
    _mk(test_db_session, "PAID/2026", source_id="2", status="paid", amount_due=0, days_overdue=0)
    r = _sync(monkeypatch, test_db_session, [_raw("DEL/2026", "1", "draft"), _raw("PAID/2026", "2", "draft")])
    assert _get(test_db_session, "DEL/2026").status == "open"
    assert _get(test_db_session, "PAID/2026").status == "paid"
    assert r["voided"] == 0 and r.get("draft_on_delivered") == 1 and r.get("draft_on_paid") == 1


def test_absent_row_checked_once_scartata_voided_rc_paid(monkeypatch, test_db_session):
    """Alla soglia di 'pagata' una sola chiamata notifiche: scartata → annullata,
    consegnata → pagata (regola storica)."""
    _mk(test_db_session, "NS/2026", source_id="ns", sdi_state="sent", missing_streak=1)
    _mk(test_db_session, "RC/2026", source_id="rc", sdi_state="sent", missing_streak=1)
    r = _sync(monkeypatch, test_db_session, [_raw("OTHER/2026", "3", None)], notif={"ns": ["NotificaScarto"], "rc": ["RicevutaConsegna"]})
    assert _get(test_db_session, "NS/2026").status == "void"
    assert _get(test_db_session, "RC/2026").status == "paid"
    assert sorted(FakeFP.calls) == ["ns", "rc"] and r["voided"] == 1 and r["paid_detected"] == 1


def test_notifications_are_fetched_before_any_write(monkeypatch, test_db_session):
    """Pre-pass: tutte le chiamate notifiche avvengono prima della prima scrittura
    (niente lock Postgres tenuti durante lo scraping)."""
    from backend.api import sync as sync_mod
    _mk(test_db_session, "OLD/2026", source_id="o")
    order = []
    orig = FakeFP.fetch_sdi_notifications

    def spy(self, doc_id):
        order.append(("http", doc_id)); return orig(self, doc_id)
    monkeypatch.setattr(FakeFP, "fetch_sdi_notifications", spy)
    orig_void = sync_mod._void_invoice

    def spy_void(*a, **k):
        order.append(("write", "void")); return orig_void(*a, **k)
    monkeypatch.setattr(sync_mod, "_void_invoice", spy_void)
    _sync(monkeypatch, test_db_session, [_raw("OLD/2026", "o", "draft"), _raw("NEW/2026", "n", "notified")], notif={"n": ["RicevutaConsegna"]})
    kinds = [k for k, _ in order]
    assert kinds.index("http") < kinds.index("write")


def test_apply_targets_row_by_key_with_duplicates(test_client, test_db_session, monkeypatch):
    import backend.connectors.fatturapro as fpmod
    monkeypatch.setattr(fpmod, "FatturaProConnector", FakeSearchFP)
    cust = Customer(ragione_sociale="CECCONI MARIO S.R.L."); test_db_session.add(cust); test_db_session.commit()
    a = _mk(test_db_session, "0800", customer_id=cust.id, source_id="a", customer_name_raw="CECCONI MARIO S.R.L.")
    b = _mk(test_db_session, "0800", customer_id=cust.id, source_id="b", amount=100.0, amount_due=100.0, customer_name_raw="CECCONI MARIO S.R.L.")
    FakeSearchFP.rows = [_fp("0800", "b", 130.0, 130.0, "Consegnato")]
    r = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    rows = {x["verdict"]: x for x in r["rows"]}
    assert rows["importo_diverso"]["platform"]["id"] == b.id and rows["duplicato"]["platform"]["id"] == a.id
    assert rows["importo_diverso"]["key"] != rows["duplicato"]["key"]
    ap = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro/apply", json={"fixes": [
        {"invoice_number": "0800", "fix": "update_amount", "key": rows["importo_diverso"]["key"]},
        {"invoice_number": "0800", "fix": "void", "key": rows["duplicato"]["key"]},
    ]}).json()
    assert {x["fix"] for x in ap["applied"]} == {"update_amount", "void"}
    test_db_session.expire_all()
    assert test_db_session.query(Invoice).get(b.id).amount == 130.0
    va = test_db_session.query(Invoice).get(a.id)
    assert va.status == "void" and va.sdi_state is None  # niente stato dell'altro documento


def test_find_existing_prefers_unpaid_active_row(test_db_session):
    from backend.api.sync import _find_existing
    paid = _mk(test_db_session, "X9/2026", source_id="p", status="paid", amount_due=0, days_overdue=0)
    openr = _mk(test_db_session, "X9/2026", source_id="o")
    assert _find_existing(test_db_session, "X9/2026", "zzz").id == openr.id
    assert _find_existing(test_db_session, "X9/2026", "p").id == paid.id


# ── Rinumerazione di FatturaPro (identità = doc_id) ────────────────────────

def test_sync_matches_by_doc_id_and_renumbers(monkeypatch, test_db_session):
    """Cecconi dal vivo: doc 4607157 era '1609' (poi sparito → 'pagata'), oggi è
    '1600'; il vecchio '1600' (doc 4606196) oggi è '1592' di un altro cliente."""
    from backend.database import Customer as C
    cec = C(ragione_sociale="CECCONI MARIO S.R.L."); bil = C(ragione_sociale="Billiken"); test_db_session.add_all([cec, bil]); test_db_session.commit()
    _mk(test_db_session, "2026/00001609/SAK - Fattura", source_id="4607157", customer_id=cec.id, customer_name_raw="CECCONI MARIO S.R.L.",
        status="paid", amount_due=0, days_overdue=0, paid_at=datetime(2026, 9, 14), amount_due_at_paid=459.42, amount=459.42)
    _mk(test_db_session, "2026/00001600/SAK - Fattura", source_id="4606196", customer_id=cec.id, customer_name_raw="CECCONI MARIO S.R.L.", amount=459.42, amount_due=459.42)
    r = _sync(monkeypatch, test_db_session, [
        _raw("2026/00001600/SAK - Fattura", "4607157", "draft", balance=459.42, name="CECCONI MARIO S.R.L."),
        _raw("2026/00001592/SAK - Fattura", "4606196", "sent", balance=410.71, name="Billiken di Okuda Atsushi"),
    ])
    rows = {x.source_id: x for x in test_db_session.query(Invoice).all()}
    assert set(rows) == {"4607157", "4606196"}  # nessuna riga nuova, nessun doppione
    a = rows["4607157"]; b = rows["4606196"]
    assert a.invoice_number == "2026/00001600/SAK - Fattura" and a.status == "void" and a.paid_at is None and "rinumerazione" in a.void_reason
    assert b.invoice_number == "2026/00001592/SAK - Fattura" and b.status == "open" and b.amount_due == 410.71 and b.sdi_state == "sent"
    assert b.customer_name_raw.startswith("Billiken") and b.customer_id is None and b.case_id is None  # scollegata: riabbina il matching
    assert r["renumbered"] == 2 and r["voided"] == 1 and r["created"] == 0
    assert test_db_session.query(ActivityLog).filter_by(action="invoice_renumbered").count() == 2
    assert test_db_session.query(ActivityLog).filter_by(action="invoice_customer_name_changed").count() == 1


def test_number_taken_over_creates_new_row_without_void(monkeypatch, test_db_session):
    """Il vecchio documento è ancora in lista (sotto altro numero) e il numero è
    passato a un documento nuovo: si crea il nuovo, si rinumera il vecchio."""
    _mk(test_db_session, "N1/2026", source_id="old", amount_due=10.0)
    r = _sync(monkeypatch, test_db_session, [
        _raw("N1/2026", "new", "notified", balance=50.0),
        _raw("N0/2026", "old", "notified", balance=10.0),
    ], notif={"new": ["RicevutaConsegna"], "old": ["RicevutaConsegna"]})
    rows = {x.source_id: (x.invoice_number, x.status) for x in test_db_session.query(Invoice).all()}
    assert rows == {"old": ("N0/2026", "open"), "new": ("N1/2026", "open")}
    assert r["voided"] == 0 and r["created"] == 1 and r["renumbered"] == 1


def test_verify_reports_renumbered_and_apply_renumbers(test_client, test_db_session, monkeypatch):
    import backend.connectors.fatturapro as fpmod
    monkeypatch.setattr(fpmod, "FatturaProConnector", FakeSearchFP)
    cust = Customer(ragione_sociale="CECCONI MARIO S.R.L."); test_db_session.add(cust); test_db_session.commit()
    inv = _mk(test_db_session, "2026/00001420/SAK - Fattura", customer_id=cust.id, source_id="4546390", amount=1044.66, amount_due=1044.66, customer_name_raw="CECCONI MARIO S.R.L.")
    FakeSearchFP.rows = [_fp("2026/00001419/SAK - Fattura", "4546390", 1044.66, 1044.66, "Consegnato")]
    FakeSearchFP.by_number = {}
    r = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    assert len(r["rows"]) == 1
    row = r["rows"][0]
    assert row["verdict"] == "rinumerata" and row["fix"] == "renumber" and row["fix_safe"] is True
    assert row["invoice_number"] == "2026/00001419/SAK - Fattura" and row["renumber_from"] == "2026/00001420/SAK - Fattura"
    a = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro/apply", json={"fixes": [{"invoice_number": row["invoice_number"], "fix": "renumber", "key": row["key"]}]}).json()
    assert [x["fix"] for x in a["applied"]] == ["renumber"]
    test_db_session.expire_all()
    assert test_db_session.query(Invoice).get(inv.id).invoice_number == "2026/00001419/SAK - Fattura"


def test_zombie_row_paid_by_absence_even_if_number_still_listed(monkeypatch, test_db_session):
    """Riga con doc_id sparito mentre il suo NUMERO è ancora in lista (preso da
    un documento rinumerato): presenza = identità, non numero → pagata per
    assenza dopo due cicli, mai doppione attivo perpetuo."""
    _mk(test_db_session, "1000/2026", source_id="A", missing_streak=1)
    _mk(test_db_session, "1001/2026", source_id="B", missing_streak=0)
    rows = [_raw("1000/2026", "B", "notified", balance=10.0), _raw("1001/2026", "C", "notified", balance=20.0)]
    _sync(monkeypatch, test_db_session, rows, notif={"B": ["RicevutaConsegna"], "C": ["RicevutaConsegna"]})
    by = {x.source_id: x for x in test_db_session.query(Invoice).all()}
    assert by["A"].status == "paid"  # assente per identità (streak 1+1)
    assert by["B"].invoice_number == "1000/2026" and by["B"].status == "open"
    assert by["C"].invoice_number == "1001/2026" and by["C"].status == "open"
    assert sum(1 for x in by.values() if x.status == "open" and x.invoice_number == "1000/2026") == 1


def test_renumber_guard_on_mass_name_changes(monkeypatch, test_db_session):
    for i in range(8):
        _mk(test_db_session, f"R{i}/2026", source_id=f"d{i}", customer_name_raw=f"CLIENTE {i}")
    # sfasamento del parser: ogni riga porta il doc della riga accanto → 8 rinumerazioni con nome diverso
    raw = [_raw(f"R{i}/2026", f"d{(i + 1) % 8}", "notified", name=f"CLIENTE {i}") for i in range(8)]
    r = _sync(monkeypatch, test_db_session, raw, notif={f"d{i}": ["RicevutaConsegna"] for i in range(8)})
    assert r.get("renumber_guard_triggered") == 8 and r.get("renumber_skipped") == 8
    assert all(x.invoice_number == f"R{int(x.source_id[1:])}/2026" for x in test_db_session.query(Invoice).all())


def test_manual_mark_paid_survives_draft_reappearance(monkeypatch, test_db_session):
    inv = _mk(test_db_session, "MP/2026", source_id="m", status="paid", amount_due=0, days_overdue=0, paid_at=datetime(2026, 9, 1))
    test_db_session.add(ActivityLog(action="fatturapro_fix_mark_paid", entity_type="invoice", entity_id=inv.id, details={})); test_db_session.commit()
    r = _sync(monkeypatch, test_db_session, [_raw("MP/2026", "m", "draft", balance=100.0)])
    assert _get(test_db_session, "MP/2026").status == "paid" and r["voided"] == 0 and r.get("draft_on_paid") == 1


# ── Righe STORICHE: doc_id fossile, identità = numero (migrazione) ─────────

def test_legacy_row_adopts_current_doc_id_by_number(monkeypatch, test_db_session):
    """Cecconi 1420 dal vivo: la riga storica porta il doc fossile 4546390 (riga
    riscritta per numero dal vecchio sync); oggi il numero 1420 è il doc
    4546517 → la riga ADOTTA il doc attuale, nessuna riga nuova, nessun
    doppione, e da qui in poi l'identità è il documento."""
    row_id = _mk(test_db_session, "2026/00001420/SAK - Fattura", source_id="4546390", doc_id_verified=False, amount=1044.66, amount_due=1044.66, customer_name_raw="CECCONI MARIO S.R.L.").id
    r = _sync(monkeypatch, test_db_session, [_raw("2026/00001420/SAK - Fattura", "4546517", "notified", balance=1044.66, name="CECCONI MARIO S.R.L.")], notif={"4546517": ["RicevutaConsegna"]})
    rows = test_db_session.query(Invoice).all()
    assert len(rows) == 1 and rows[0].id == row_id
    assert rows[0].source_id == "4546517" and rows[0].doc_id_verified is True and rows[0].sdi_state == "consegnata"
    assert r["created"] == 0 and r["voided"] == 0 and r.get("doc_id_adopted") == 1 and r.get("renumbered", 0) == 0
    assert test_db_session.query(ActivityLog).filter_by(action="doc_id_adopted").count() == 1


def test_legacy_fossil_released_when_another_legacy_row_adopts_it(monkeypatch, test_db_session):
    """Due righe storiche: A porta come fossile il doc che oggi ha il numero di B.
    B lo adotta, A lo rilascia (source_id None) e poi adotta il suo."""
    a_id = _mk(test_db_session, "A/2026", source_id="dB", doc_id_verified=False).id
    b_id = _mk(test_db_session, "B/2026", source_id="dX", doc_id_verified=False).id
    r = _sync(monkeypatch, test_db_session, [_raw("B/2026", "dB", "notified", balance=10.0), _raw("A/2026", "dA", "notified", balance=20.0)],
              notif={"dB": ["RicevutaConsegna"], "dA": ["RicevutaConsegna"]})
    a = test_db_session.query(Invoice).get(a_id); b = test_db_session.query(Invoice).get(b_id)
    assert (b.source_id, b.doc_id_verified, b.amount_due) == ("dB", True, 10.0)
    assert (a.source_id, a.doc_id_verified, a.amount_due) == ("dA", True, 20.0)
    assert r.get("doc_id_adopted") == 2 and r["created"] == 0 and r["voided"] == 0


def test_legacy_renumbered_draft_without_number_holder(monkeypatch, test_db_session):
    """Cecconi 1609 dal vivo: riga storica 'pagata per assenza' col doc 4607157;
    oggi quel doc si chiama 1600 e nessuna riga ha il numero 1600 → è lo stesso
    documento rinumerato: rinumerata, e (bozza con residuo) annullata."""
    _mk(test_db_session, "2026/00001609/SAK - Fattura", source_id="4607157", doc_id_verified=False, status="paid", amount_due=0, days_overdue=0,
        paid_at=datetime(2026, 9, 14), amount_due_at_paid=459.42, amount=459.42, customer_name_raw="CECCONI MARIO S.R.L.")
    r = _sync(monkeypatch, test_db_session, [_raw("2026/00001600/SAK - Fattura", "4607157", "draft", balance=459.42, name="CECCONI MARIO S.R.L.")])
    rows = test_db_session.query(Invoice).all()
    assert len(rows) == 1
    assert rows[0].invoice_number == "2026/00001600/SAK - Fattura" and rows[0].status == "void" and rows[0].paid_at is None
    assert r["renumbered"] == 1 and r["voided"] == 1 and r["created"] == 0


def test_duplicate_created_by_previous_cycle_is_voided_in_favour_of_legacy_holder(monkeypatch, test_db_session):
    """Il ciclo precedente ha creato una riga nuova (verificata, senza storia)
    per il numero N mentre la storica con quel numero portava un fossile: vince
    la storica (ha i solleciti), che adotta il doc; il doppione si annulla."""
    from backend.database import RecoveryAction, RecoveryActionInvoice as RAI
    cust = Customer(ragione_sociale="Storica SRL"); test_db_session.add(cust); test_db_session.commit()
    legacy_id = _mk(test_db_session, "N/2026", source_id="fossil", doc_id_verified=False, amount_due=100.0, customer_id=cust.id).id
    act = RecoveryAction(customer_id=cust.id, action_type="first_contact", channel="whatsapp_copy", completed_at=datetime(2026, 9, 1), invoice_ids=[legacy_id])
    test_db_session.add(act); test_db_session.commit(); test_db_session.add(RAI(action_id=act.id, invoice_id=legacy_id)); test_db_session.commit()
    dup_id = _mk(test_db_session, "N/2026", source_id="real", doc_id_verified=True, amount_due=100.0, sdi_state="consegnata", customer_id=cust.id).id
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "real", "notified", balance=90.0)])
    legacy = test_db_session.query(Invoice).get(legacy_id); dup = test_db_session.query(Invoice).get(dup_id)
    assert (legacy.source_id, legacy.doc_id_verified, legacy.status, legacy.amount_due) == ("real", True, "open", 90.0)
    assert dup.status == "void" and "doppione" in dup.void_reason
    assert r.get("dup_voided") == 1 and r["created"] == 0


def test_legacy_absence_is_by_number(monkeypatch, test_db_session):
    """Riga storica (fossile): assente solo se il NUMERO non è in lista."""
    _mk(test_db_session, "L/2026", source_id="fossil", doc_id_verified=False, missing_streak=1)
    _sync(monkeypatch, test_db_session, [_raw("L/2026", "real", None)])
    assert _get(test_db_session, "L/2026").status == "open"  # numero presente → adotta, non assente
    _mk(test_db_session, "M/2026", source_id="fossil2", doc_id_verified=False, missing_streak=1)
    r = _sync(monkeypatch, test_db_session, [_raw("L/2026", "real", None)])
    assert _get(test_db_session, "M/2026").status == "paid" and r["paid_detected"] == 1


# ── Review della migrazione (piano dopo il pre-pass, destinatario, fossili) ──

def test_stale_by_doc_after_adoption_does_not_void_a_legit_row(monkeypatch, test_db_session):
    """B1: L ('N', fossile F) e L2 ('N2', fossile D = doc attuale di N). N adotta D;
    quando arriva la riga (N2, D2) l'owner stantio di D non deve essere trattato
    come doppione: L2 adotta D2, nessun annullamento."""
    l_id = _mk(test_db_session, "N/2026", source_id="F", doc_id_verified=False, payment_pending="assegno", customer_name_raw="ACME SRL").id
    l2_id = _mk(test_db_session, "N2/2026", source_id="D", doc_id_verified=False, payment_pending="assegno", customer_name_raw="ACME SRL").id
    for order in (["N", "N2"], ["N2", "N"]):
        rows = {"N": _raw("N/2026", "D", "notified", balance=10.0), "N2": _raw("N2/2026", "D2", "notified", balance=20.0)}
        r = _sync(monkeypatch, test_db_session, [rows[k] for k in order], notif={"D": ["RicevutaConsegna"], "D2": ["RicevutaConsegna"]})
        l = test_db_session.query(Invoice).get(l_id); l2 = test_db_session.query(Invoice).get(l2_id)
        assert (l.source_id, l.status, l.payment_pending) == ("D", "open", "assegno")
        assert (l2.source_id, l2.status, l2.payment_pending) == ("D2", "open", "assegno")
        assert r["voided"] == 0 and r["created"] == 0
        assert test_db_session.query(Invoice).count() == 2


def test_adoption_requires_same_recipient_else_orphan_and_new_row(monkeypatch, test_db_session):
    """M1: storica 'N' intestata ALFA; su FatturaPro 'N' è ora di BETA → la storica
    non adotta (orfana, annullata), il documento di BETA entra come riga nuova."""
    from backend.database import Customer as C
    alfa = C(ragione_sociale="ALFA SRL"); test_db_session.add(alfa); test_db_session.commit()
    alfa_id = alfa.id
    old_id = _mk(test_db_session, "N/2026", source_id="fossil", doc_id_verified=False, customer_id=alfa_id, customer_name_raw="ALFA SRL").id
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "D", "notified", balance=70.0, name="BETA SRL")], notif={"D": ["RicevutaConsegna"]})
    old = test_db_session.query(Invoice).get(old_id)
    # orfana: nessuna scrittura immediata, ma è ASSENTE (numero di altro destinatario) → streak
    assert old.status == "open" and old.missing_streak == 1 and r.get("legacy_orphans") == 1 and r["voided"] == 0
    assert test_db_session.query(ActivityLog).filter_by(action="legacy_orphan").count() == 1
    new = _get(test_db_session, "N/2026", source_id="D")
    assert new.customer_name_raw == "BETA SRL" and new.customer_id != alfa_id and new.doc_id_verified is True
    # secondo ciclo: regola storica dell'assenza (pagata), una sola riga nuova, nessun loop
    r2 = _sync(monkeypatch, test_db_session, [_raw("N/2026", "D", "notified", balance=70.0, name="BETA SRL")])
    old = test_db_session.query(Invoice).get(old_id)
    assert old.status == "paid" and r2["created"] == 0 and r2["voided"] == 0
    assert test_db_session.query(Invoice).filter_by(invoice_number="N/2026").count() == 2


def test_adoption_resets_sdi_state_and_rechecks(monkeypatch, test_db_session):
    """M3: lo stato SDI memorizzato era del documento fossile: all'adozione si
    azzera e il pre-pass ricontrolla il documento adottato (bozza → annullata)."""
    _mk(test_db_session, "N/2026", source_id="fossil", doc_id_verified=False, sdi_state="consegnata", customer_name_raw="ACME SRL")
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "D", "draft", balance=100.0)])
    inv = _get(test_db_session, "N/2026")
    assert inv.source_id == "D" and inv.status == "void" and inv.sdi_state == "draft" and r["voided"] == 1


def test_release_of_fossil_from_paid_by_absence_row_voids_it(monkeypatch, test_db_session):
    """Cecconi dal vivo con la riga 1600 storica: la 1609 'pagata per assenza'
    porta come fossile il doc che oggi è la 1600 (adottato dalla storica 1600):
    non era un incasso → annullata; nessuna riga nuova."""
    _mk(test_db_session, "2026/00001600/SAK - Fattura", source_id="4606196", doc_id_verified=False, amount=459.42, amount_due=459.42, customer_name_raw="CECCONI MARIO S.R.L.")
    _mk(test_db_session, "2026/00001609/SAK - Fattura", source_id="4607157", doc_id_verified=False, status="paid", amount_due=0, days_overdue=0,
        paid_at=datetime(2026, 9, 14), amount_due_at_paid=459.42, amount=459.42, customer_name_raw="CECCONI MARIO S.R.L.")
    r = _sync(monkeypatch, test_db_session, [_raw("2026/00001600/SAK - Fattura", "4607157", "draft", balance=459.42, name="CECCONI MARIO S.R.L.")])
    rows = {x.invoice_number[-20:-14]: x for x in test_db_session.query(Invoice).all()}
    a = _get(test_db_session, "2026/00001600/SAK - Fattura"); b = _get(test_db_session, "2026/00001609/SAK - Fattura")
    assert (a.source_id, a.status, a.sdi_state) == ("4607157", "void", "draft")   # bozza adottata → annullata
    assert b.status == "void" and b.paid_at is None and b.source_id is None and "rinumerata" in b.void_reason
    assert r["created"] == 0 and r.get("fossil_released") == 1 and r["voided"] == 2 and len(rows) == 2


def test_live_state_legacy_duplicates_resolved_by_history(monkeypatch, test_db_session):
    """Stato live: doppione creato ieri (doc reale, NON verificato dopo la
    migration) + storica coi solleciti e lo stesso numero → vince la storica."""
    from backend.database import RecoveryAction, RecoveryActionInvoice as RAI
    cust = Customer(ragione_sociale="Storica2 SRL"); test_db_session.add(cust); test_db_session.commit()
    legacy_id = _mk(test_db_session, "N/2026", source_id="fossil", doc_id_verified=False, customer_id=cust.id, customer_name_raw="Storica2 SRL").id
    act = RecoveryAction(customer_id=cust.id, action_type="first_contact", channel="whatsapp_copy", completed_at=datetime(2026, 9, 1), invoice_ids=[legacy_id])
    test_db_session.add(act); test_db_session.commit(); test_db_session.add(RAI(action_id=act.id, invoice_id=legacy_id)); test_db_session.commit()
    dup_id = _mk(test_db_session, "N/2026", source_id="real", doc_id_verified=False, customer_id=cust.id, customer_name_raw="Storica2 SRL").id
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "real", "notified", balance=90.0, name="Storica2 SRL")], notif={"real": ["RicevutaConsegna"]})
    legacy = test_db_session.query(Invoice).get(legacy_id); dup = test_db_session.query(Invoice).get(dup_id)
    assert (legacy.source_id, legacy.doc_id_verified, legacy.status, legacy.amount_due) == ("real", True, "open", 90.0)
    assert dup.status == "void" and r.get("dup_voided") == 1


def test_adoption_writes_happen_after_notification_prepass(monkeypatch, test_db_session):
    from backend.api import sync as sync_mod
    _mk(test_db_session, "N/2026", source_id="fossil", doc_id_verified=False, customer_name_raw="ACME SRL")
    order = []
    orig = FakeFP.fetch_sdi_notifications

    def spy(self, doc_id):
        order.append("http"); return orig(self, doc_id)
    monkeypatch.setattr(FakeFP, "fetch_sdi_notifications", spy)
    orig_add = test_db_session.add

    def spy_add(obj):
        if isinstance(obj, ActivityLog) and obj.action == "doc_id_adopted":
            order.append("adopt")
        return orig_add(obj)
    monkeypatch.setattr(test_db_session, "add", spy_add)
    _sync(monkeypatch, test_db_session, [_raw("N/2026", "D", "notified", balance=10.0)], notif={"D": ["RicevutaConsegna"]})
    assert "http" in order and "adopt" in order and order.index("http") < order.index("adopt")


def test_verify_duplicates_follow_history_and_legacy_renumber(test_client, test_db_session, monkeypatch):
    """M4: la verifica per cliente decide come il sync: fra doppioni vince chi
    ha i solleciti; una storica il cui doc è su FatturaPro sotto un numero
    LIBERO è 'rinumerata'."""
    import backend.connectors.fatturapro as fpmod
    from backend.database import RecoveryAction, RecoveryActionInvoice as RAI
    monkeypatch.setattr(fpmod, "FatturaProConnector", FakeSearchFP)
    cust = Customer(ragione_sociale="CECCONI MARIO S.R.L."); test_db_session.add(cust); test_db_session.commit()
    legacy = _mk(test_db_session, "0800", customer_id=cust.id, source_id="fossil", doc_id_verified=False, customer_name_raw="CECCONI MARIO S.R.L.")
    act = RecoveryAction(customer_id=cust.id, action_type="first_contact", channel="whatsapp_copy", completed_at=datetime(2026, 9, 1), invoice_ids=[legacy.id])
    test_db_session.add(act); test_db_session.commit(); test_db_session.add(RAI(action_id=act.id, invoice_id=legacy.id)); test_db_session.commit()
    dup = _mk(test_db_session, "0800", customer_id=cust.id, source_id="real", doc_id_verified=True, customer_name_raw="CECCONI MARIO S.R.L.")
    ren = _mk(test_db_session, "1609", customer_id=cust.id, source_id="4607157", doc_id_verified=False, customer_name_raw="CECCONI MARIO S.R.L.")
    FakeSearchFP.rows = [_fp("0800", "real", 100.0, 100.0, "Consegnato"), _fp("1600", "4607157", 100.0, 100.0, "Consegnato")]
    FakeSearchFP.by_number = {}
    r = test_client.post(f"/api/customers/{cust.id}/verify-fatturapro").json()
    by = {(x["verdict"], (x.get("platform") or {}).get("id")): x for x in r["rows"]}
    assert ("ok", legacy.id) in by and ("duplicato", dup.id) in by
    assert ("rinumerata", ren.id) in by and by[("rinumerata", ren.id)]["invoice_number"] == "1600"


# ── Review di secondo giro della migrazione ─────────────────────────────────

def _act(session, inv_id, cust_id):
    from backend.database import RecoveryAction, RecoveryActionInvoice as RAI
    a = RecoveryAction(customer_id=cust_id, action_type="first_contact", channel="whatsapp_copy", completed_at=datetime(2026, 9, 1), invoice_ids=[inv_id])
    session.add(a); session.commit(); session.add(RAI(action_id=a.id, invoice_id=inv_id)); session.commit()


def test_paid_legacy_with_history_of_other_recipient_no_create_void_loop(monkeypatch, test_db_session):
    """B1: storica PAGATA con solleciti di ALFA, numero N ora del doc D di BETA:
    una sola riga nuova, stabile su 3 cicli (mai doppione/annullamento)."""
    from backend.database import Customer as C
    alfa = C(ragione_sociale="ALFA SRL"); test_db_session.add(alfa); test_db_session.commit(); alfa_id = alfa.id
    paid_id = _mk(test_db_session, "N/2026", source_id="fossil", doc_id_verified=False, status="paid", amount_due=0, days_overdue=0,
                  customer_id=alfa_id, customer_name_raw="ALFA SRL", paid_at=datetime(2026, 8, 1)).id
    _act(test_db_session, paid_id, alfa_id)
    for cycle in range(3):
        r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "D", "notified", balance=70.0, name="BETA SRL")], notif={"D": ["RicevutaConsegna"]})
        assert r["voided"] == 0 and r["created"] == (1 if cycle == 0 else 0)
    rows = test_db_session.query(Invoice).filter_by(invoice_number="N/2026").all()
    assert sorted((x.status, x.source_id) for x in rows) == [("open", "D"), ("paid", "fossil")]
    assert test_db_session.query(ActivityLog).filter_by(action="invoice_voided").count() == 0


def test_doc_coincident_legacy_row_gets_verified(monkeypatch, test_db_session):
    """M1: storica il cui fossile coincide col doc attuale → confermata (verificata)."""
    _mk(test_db_session, "N/2026", source_id="D", doc_id_verified=False, customer_name_raw="ACME SRL")
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "D", "notified", balance=10.0)], notif={"D": ["RicevutaConsegna"]})
    inv = _get(test_db_session, "N/2026")
    assert inv.doc_id_verified is True and inv.sdi_state == "consegnata" and r.get("doc_id_confirmed") == 1


def test_phantom_paid_sharing_doc_with_holder_is_voided(monkeypatch, test_db_session):
    """M2: A ('1609', doc D, pagata per assenza) e B ('1600', doc D, storica aperta),
    FP ('1600', D): B confermata, A annullata (non era un incasso)."""
    a_id = _mk(test_db_session, "1609", source_id="D", doc_id_verified=False, status="paid", amount_due=0, days_overdue=0, paid_at=datetime(2026, 9, 14), customer_name_raw="CECCONI MARIO S.R.L.").id
    b_id = _mk(test_db_session, "1600", source_id="D", doc_id_verified=False, customer_name_raw="CECCONI MARIO S.R.L.").id
    r = _sync(monkeypatch, test_db_session, [_raw("1600", "D", "notified", balance=100.0, name="CECCONI MARIO S.R.L.")], notif={"D": ["RicevutaConsegna"]})
    a = test_db_session.query(Invoice).get(a_id); b = test_db_session.query(Invoice).get(b_id)
    assert (b.doc_id_verified, b.status) == (True, "open")
    assert a.status == "void" and a.paid_at is None and a.source_id is None and r["voided"] == 1
    assert len([x for x in test_db_session.query(Invoice).all() if x.status != "void" and x.source_id == "D"]) == 1


def test_row_with_assegno_is_history_and_never_a_duplicate(monkeypatch, test_db_session):
    """M3: storica con assegno in mano (senza righe azione) vs doppione con un
    sollecito: la storica non viene mai annullata."""
    from backend.database import Customer as C
    c = C(ragione_sociale="ACME SRL"); test_db_session.add(c); test_db_session.commit(); cid = c.id
    legacy_id = _mk(test_db_session, "N/2026", source_id="fossil", doc_id_verified=False, customer_id=cid, customer_name_raw="ACME SRL", payment_pending="assegno").id
    dup_id = _mk(test_db_session, "N/2026", source_id="D", doc_id_verified=True, customer_id=cid, customer_name_raw="ACME SRL").id
    _act(test_db_session, dup_id, cid)
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "D", "notified", balance=100.0, name="ACME SRL")], notif={"D": ["RicevutaConsegna"]})
    legacy = test_db_session.query(Invoice).get(legacy_id); dup = test_db_session.query(Invoice).get(dup_id)
    assert legacy.status == "open" and legacy.payment_pending == "assegno" and dup.status == "open"
    assert r["voided"] == 0 and r.get("number_conflicts") == 1
    assert test_db_session.query(ActivityLog).filter_by(action="invoice_number_conflict").count() == 1


def test_recipient_comparison_ignores_punctuation_not_legal_form(monkeypatch, test_db_session):
    """M4: 'CECCONI MARIO S.R.L.' = 'CECCONI MARIO SRL' (adotta); 'ROSSI SRL' ≠ 'ROSSI SPA'."""
    _mk(test_db_session, "A/2026", source_id="fossil", doc_id_verified=False, customer_name_raw="CECCONI MARIO S.R.L.", payment_pending="assegno")
    _mk(test_db_session, "B/2026", source_id="fossil2", doc_id_verified=False, customer_name_raw="ROSSI SRL")
    r = _sync(monkeypatch, test_db_session, [
        _raw("A/2026", "dA", "notified", balance=10.0, name="CECCONI MARIO SRL"),
        _raw("B/2026", "dB", "notified", balance=20.0, name="ROSSI SPA"),
    ], notif={"dA": ["RicevutaConsegna"], "dB": ["RicevutaConsegna"]})
    a = _get(test_db_session, "A/2026"); b_old = _get(test_db_session, "B/2026", source_id="fossil2")
    assert a.source_id == "dA" and a.payment_pending == "assegno" and r["voided"] == 0
    assert b_old.missing_streak == 1 and _get(test_db_session, "B/2026", source_id="dB").customer_name_raw == "ROSSI SPA"


def test_free_number_with_legacy_owner_of_other_recipient_is_not_renumbered(monkeypatch, test_db_session):
    """M5: riga di X (assegno) col fossile D; FP (N, D) di BETA, numero libero:
    la riga di X NON viene rinumerata/ripuntata; BETA entra come riga nuova."""
    x_id = _mk(test_db_session, "OLD/2026", source_id="D", doc_id_verified=False, customer_name_raw="X SRL", payment_pending="assegno").id
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "D", "notified", balance=50.0, name="BETA SRL")], notif={"D": ["RicevutaConsegna"]})
    x = test_db_session.query(Invoice).get(x_id)
    assert x.invoice_number == "OLD/2026" and x.status == "open" and x.payment_pending == "assegno" and r.get("renumbered", 0) == 0
    assert _get(test_db_session, "N/2026").customer_name_raw == "BETA SRL" and r["created"] == 1


def test_two_rows_with_history_same_number_no_automatic_write(monkeypatch, test_db_session):
    """M6: storica con solleciti + doppione con solleciti: nessuna riga rilasciata
    né annullata; conflitto loggato; nessuno zombie (nessun doppio conteggio nuovo)."""
    from backend.database import Customer as C
    c = C(ragione_sociale="ACME SRL"); test_db_session.add(c); test_db_session.commit(); cid = c.id
    l_id = _mk(test_db_session, "N/2026", source_id="fossil", doc_id_verified=False, customer_id=cid, customer_name_raw="ACME SRL").id
    d_id = _mk(test_db_session, "N/2026", source_id="D", doc_id_verified=True, customer_id=cid, customer_name_raw="ACME SRL").id
    _act(test_db_session, l_id, cid); _act(test_db_session, d_id, cid)
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "D", "notified", balance=90.0, name="ACME SRL")], notif={"D": ["RicevutaConsegna"]})
    l = test_db_session.query(Invoice).get(l_id); d = test_db_session.query(Invoice).get(d_id)
    assert l.status == "open" and d.status == "open" and l.source_id == "fossil" and d.source_id == "D"
    assert r["voided"] == 0 and r.get("number_conflicts") == 1 and r.get("fossil_released", 0) == 0


# ── Terzo giro ──────────────────────────────────────────────────────────────

def test_fossil_twin_absent_is_voided_not_paid(monkeypatch, test_db_session):
    """A: blocco scalato L1(N1,F1) L2(N2,F2); FP (N0,F1) (N1,F2): L1 adotta F2;
    L2 (numero N2 non più in lista, fossile F2 vivo sotto N1) è la gemella
    fossile senza storia → annullata come rinumerata, mai 'pagata'."""
    l1 = _mk(test_db_session, "N1/2026", source_id="F1", doc_id_verified=False, customer_name_raw="ACME SRL").id
    l2 = _mk(test_db_session, "N2/2026", source_id="F2", doc_id_verified=False, customer_name_raw="ACME SRL", missing_streak=1).id
    r = _sync(monkeypatch, test_db_session, [_raw("N0/2026", "F1", "notified", balance=100.0), _raw("N1/2026", "F2", "notified", balance=100.0)],
              notif={"F1": ["RicevutaConsegna"], "F2": ["RicevutaConsegna"]})
    a = test_db_session.query(Invoice).get(l1); b = test_db_session.query(Invoice).get(l2)
    assert (a.source_id, a.status, a.amount_due) == ("F2", "open", 100.0)
    assert b.status == "void" and "rinumerata" in b.void_reason and b.paid_at is None
    assert r["paid_detected"] == 0 and r["created"] == 1  # N0/F1 entra come riga nuova


def test_conflict_row_is_never_paid_by_absence(monkeypatch, test_db_session):
    """A: gemella fossile CON storia: conflitto loggato e, finché aperto, mai
    'pagata per assenza'."""
    from backend.database import Customer as C
    c = C(ragione_sociale="ACME SRL"); test_db_session.add(c); test_db_session.commit(); cid = c.id
    _mk(test_db_session, "N1/2026", source_id="F1", doc_id_verified=False, customer_name_raw="ACME SRL", customer_id=cid)
    l2 = _mk(test_db_session, "N2/2026", source_id="F2", doc_id_verified=False, customer_name_raw="ACME SRL", customer_id=cid, missing_streak=1).id
    _act(test_db_session, l2, cid)
    for _ in range(2):
        r = _sync(monkeypatch, test_db_session, [_raw("N1/2026", "F2", "notified", balance=100.0)], notif={"F2": ["RicevutaConsegna"]})
        b = test_db_session.query(Invoice).get(l2)
        assert b.status == "open" and r["paid_detected"] == 0 and r.get("number_conflicts") == 1


def test_renumber_guard_ignores_cosmetic_name_changes(monkeypatch, test_db_session):
    """B: sei owner storici con 'S.R.L.' vs 'SRL' non fanno scattare la guardia;
    la rinumerazione legittima procede e il documento entra."""
    for i in range(6):
        _mk(test_db_session, f"OLD{i}/2026", source_id=f"d{i}", doc_id_verified=False, customer_name_raw=f"CLIENTE {i} S.R.L.")
    raw = [_raw(f"NEW{i}/2026", f"d{i}", "notified", balance=100.0, name=f"CLIENTE {i} SRL") for i in range(6)]
    r = _sync(monkeypatch, test_db_session, raw, notif={f"d{i}": ["RicevutaConsegna"] for i in range(6)})
    assert r.get("renumber_guard_triggered") is None and r["renumbered"] == 6 and r.get("renumber_skipped", 0) == 0
    assert all(x.invoice_number.startswith("NEW") and x.customer_id is None or x.customer_id is None for x in test_db_session.query(Invoice).all())


def test_verify_history_includes_assegno(test_db_session):
    """K: la verifica per cliente usa lo stesso predicato di storia del sync."""
    from backend.api.customers import _action_counts
    cust = Customer(ragione_sociale="ACME SRL"); test_db_session.add(cust); test_db_session.commit()
    legacy = _mk(test_db_session, "0800", customer_id=cust.id, source_id="fossil", doc_id_verified=False, payment_pending="assegno")
    dup = _mk(test_db_session, "0800", customer_id=cust.id, source_id="real", doc_id_verified=True)
    _act(test_db_session, dup.id, cust.id)
    res = compare_documents([legacy, dup], [_fp("0800", "real", 100.0, 100.0, "Consegnato")], action_counts=_action_counts(test_db_session, [legacy, dup]))
    v = {r["verdict"]: r["platform"]["id"] for r in res["rows"]}
    assert v["ok"] == legacy.id and v["duplicato"] == dup.id


def test_legacy_owner_with_present_number_adopts_instead_of_renumber(monkeypatch, test_db_session):
    """Determinismo: L('N', fossile F) col numero N ancora in lista (doc D) e F
    vivo sotto N': L adotta D (per lei vale il numero), F entra come riga nuova —
    in qualunque ordine."""
    for order in (0, 1):
        for x in test_db_session.query(Invoice).all():
            test_db_session.delete(x)
        test_db_session.commit()
        l_id = _mk(test_db_session, "N/2026", source_id="F", doc_id_verified=False, customer_name_raw="ACME SRL").id
        rows = [_raw("N/2026", "D", "notified", balance=10.0), _raw("NX/2026", "F", "notified", balance=30.0)]
        if order:
            rows.reverse()
        r = _sync(monkeypatch, test_db_session, rows, notif={"D": ["RicevutaConsegna"], "F": ["RicevutaConsegna"]})
        l = test_db_session.query(Invoice).get(l_id)
        assert (l.invoice_number, l.source_id, l.doc_id_verified) == ("N/2026", "D", True)
        assert _get(test_db_session, "NX/2026").source_id == "F" and r["created"] == 1 and r["voided"] == 0


# ── Quarto giro ─────────────────────────────────────────────────────────────

def test_absent_legacy_with_live_fossil_doc_is_never_paid(monkeypatch, test_db_session):
    """B1: L(N1,F1,ALFA); FP (N1,F2,BETA consegnata) + (N0,F1,ALFA BOZZA): F1 non
    entra (bozza) ma è vivo → L non va mai 'pagata': senza storia annullata."""
    l_id = _mk(test_db_session, "N1/2026", source_id="F1", doc_id_verified=False, customer_name_raw="ALFA SRL", missing_streak=1).id
    r = _sync(monkeypatch, test_db_session, [_raw("N1/2026", "F2", "notified", balance=20.0, name="BETA SRL"), _raw("N0/2026", "F1", "draft", balance=100.0, name="ALFA SRL")],
              notif={"F2": ["RicevutaConsegna"]})
    l = test_db_session.query(Invoice).get(l_id)
    assert l.status == "void" and l.paid_at is None and r["paid_detected"] == 0 and r["created"] == 1


def test_absent_legacy_with_live_fossil_doc_and_history_stays_open_in_conflict(monkeypatch, test_db_session):
    from backend.database import Customer as C
    c = C(ragione_sociale="ALFA SRL"); test_db_session.add(c); test_db_session.commit(); cid = c.id
    l_id = _mk(test_db_session, "N1/2026", source_id="F1", doc_id_verified=False, customer_name_raw="ALFA SRL", customer_id=cid, missing_streak=1).id
    _act(test_db_session, l_id, cid)
    for _ in range(2):
        r = _sync(monkeypatch, test_db_session, [_raw("N1/2026", "F2", "notified", balance=20.0, name="BETA SRL"), _raw("N0/2026", "F1", "draft", balance=100.0, name="ALFA SRL")],
                  notif={"F2": ["RicevutaConsegna"]})
        l = test_db_session.query(Invoice).get(l_id)
        assert l.status == "open" and r["paid_detected"] == 0 and r["voided"] == 0


def test_paid_row_never_becomes_holder_over_open_nor_duplicate(monkeypatch, test_db_session):
    """B2: P(N, Dold, pagata a MANO) + O(N, D, aperta creata da PR #35), FP (N, D):
    O resta l'holder (confermata), P non viene né riaperta né annullata."""
    p_id = _mk(test_db_session, "N/2026", source_id="Dold", doc_id_verified=False, status="paid", amount_due=0, days_overdue=0, paid_at=datetime(2026, 8, 1), customer_name_raw="ACME SRL").id
    test_db_session.add(ActivityLog(action="fatturapro_fix_mark_paid", entity_type="invoice", entity_id=p_id, details={})); test_db_session.commit()
    o_id = _mk(test_db_session, "N/2026", source_id="D", doc_id_verified=False, customer_name_raw="ACME SRL").id
    r = _sync(monkeypatch, test_db_session, [_raw("N/2026", "D", "notified", balance=90.0, name="ACME SRL")], notif={"D": ["RicevutaConsegna"]})
    p = test_db_session.query(Invoice).get(p_id); o = test_db_session.query(Invoice).get(o_id)
    assert p.status == "paid" and p.paid_at is not None
    assert (o.status, o.doc_id_verified, o.amount_due) == ("open", True, 90.0) and r["voided"] == 0


def test_fossil_twin_resolved_in_same_cycle_as_creation(monkeypatch, test_db_session):
    """M3: gemella orfana senza storia il cui doc entra come riga NUOVA in questo
    ciclo: annullata subito (nessun ciclo di credito doppio)."""
    l_id = _mk(test_db_session, "N2/2026", source_id="F", doc_id_verified=False, customer_name_raw="ALFA SRL", missing_streak=1).id
    r = _sync(monkeypatch, test_db_session, [_raw("N2/2026", "G", "notified", balance=5.0, name="BETA SRL"), _raw("N1/2026", "F", "notified", balance=100.0, name="ALFA SRL")],
              notif={"F": ["RicevutaConsegna"], "G": ["RicevutaConsegna"]})
    l = test_db_session.query(Invoice).get(l_id)
    assert l.status == "void" and r["created"] == 2 and r["paid_detected"] == 0
    assert len([x for x in test_db_session.query(Invoice).all() if x.status != "void"]) == 2


# ── Quinto giro: l'importo discrimina "stesso documento" da "altra fattura" ─

def test_legit_paid_in_shifted_block_is_preserved_when_amount_differs(monkeypatch, test_db_session):
    """B1: riga storica PAGATA per assenza (con solleciti) il cui fossile è vivo
    sotto un altro numero ma con IMPORTO diverso: era un'altra fattura, davvero
    incassata → resta pagata (solo il fossile viene lasciato)."""
    from backend.database import Customer as C
    c = C(ragione_sociale="ACME SRL"); test_db_session.add(c); test_db_session.commit(); cid = c.id
    p_id = _mk(test_db_session, "N2/2026", source_id="F", doc_id_verified=False, status="paid", amount=300.0, amount_due=0, days_overdue=0,
               paid_at=datetime(2026, 8, 1), amount_due_at_paid=300.0, customer_name_raw="ACME SRL", customer_id=cid).id
    _act(test_db_session, p_id, cid)
    _mk(test_db_session, "N1/2026", source_id="E", doc_id_verified=False, amount=100.0, amount_due=100.0, customer_name_raw="ACME SRL", customer_id=cid)
    r = _sync(monkeypatch, test_db_session, [_raw("N1/2026", "F", "notified", balance=100.0, name="ACME SRL")], notif={"F": ["RicevutaConsegna"]})
    p = test_db_session.query(Invoice).get(p_id)
    assert p.status == "paid" and p.paid_at is not None and p.amount_due_at_paid == 300.0 and p.source_id is None
    assert r["voided"] == 0


def test_open_absent_with_live_fossil_but_different_amount_follows_number_rule(monkeypatch, test_db_session):
    """B1 (aperta): fossile vivo con importo diverso = altra fattura, assente
    perché incassata → regola storica (pagata allo streak), MAI annullata."""
    l_id = _mk(test_db_session, "N2/2026", source_id="F", doc_id_verified=False, amount=300.0, amount_due=300.0, customer_name_raw="ACME SRL", missing_streak=1).id
    r = _sync(monkeypatch, test_db_session, [_raw("N1/2026", "F", "notified", balance=100.0, name="ACME SRL")], notif={"F": ["RicevutaConsegna"]})
    l = test_db_session.query(Invoice).get(l_id)
    assert l.status == "paid" and l.amount_due_at_paid == 300.0 and r["voided"] == 0 and r["paid_detected"] == 1


def test_free_number_owner_with_different_amount_is_not_hijacked(monkeypatch, test_db_session):
    """Hijack: owner storico (fossile D, importo 300) con numero libero su FP per D
    (importo 100): non è lo stesso documento → riga nuova, l'owner resta."""
    o_id = _mk(test_db_session, "N2/2026", source_id="D", doc_id_verified=False, amount=300.0, amount_due=300.0, customer_name_raw="ACME SRL").id
    r = _sync(monkeypatch, test_db_session, [_raw("N1/2026", "D", "notified", balance=100.0, name="ACME SRL")], notif={"D": ["RicevutaConsegna"]})
    o = test_db_session.query(Invoice).get(o_id)
    assert o.invoice_number == "N2/2026" and o.amount == 300.0 and r.get("renumbered", 0) == 0 and r["created"] == 1


def test_verify_flags_other_recipient_without_fix(test_db_session):
    cust = Customer(ragione_sociale="ALFA SRL"); test_db_session.add(cust); test_db_session.commit()
    pl = _mk(test_db_session, "0900", customer_id=cust.id, source_id="fossil", doc_id_verified=False, customer_name_raw="ALFA SRL", amount=100.0)
    beta = dict(_fp("0900", "G", 250.0, 250.0, "Consegnato")); beta["customer_name"] = "BETA SRL"
    res = compare_documents([pl], [beta])
    assert res["rows"][0]["verdict"] == "altro_destinatario" and res["rows"][0]["fix"] is None


# ── Sesto giro: fantasmi confermati dal numero, grazia unica ────────────────

def test_phantom_paid_confirmed_by_number_lookup(monkeypatch, test_db_session):
    """M2: pagata per assenza con fossile vivo, stesso importo e destinatario:
    - se FatturaPro conosce ancora il suo NUMERO → incasso vero, resta pagata;
    - se la ricerca fallisce → nessuna scrittura (si riprova);
    - se il numero non esiste più → fantasma, annullata (Cecconi 1609)."""
    def scenario():
        for x in test_db_session.query(Invoice).all():
            test_db_session.delete(x)
        for x in test_db_session.query(ActivityLog).all():
            test_db_session.delete(x)
        test_db_session.commit()
        p = _mk(test_db_session, "2026/00001609/SAK - Fattura", source_id="D", doc_id_verified=False, status="paid", amount=459.42, amount_due=0, days_overdue=0,
                paid_at=datetime(2026, 9, 14), amount_due_at_paid=459.42, customer_name_raw="CECCONI MARIO S.R.L.").id
        _mk(test_db_session, "2026/00001600/SAK - Fattura", source_id="E", doc_id_verified=False, amount=459.42, amount_due=459.42, customer_name_raw="CECCONI MARIO S.R.L.")
        return p
    row = _raw("2026/00001600/SAK - Fattura", "D", "notified", balance=459.42, name="CECCONI MARIO S.R.L.")
    # 1) numero ancora esistente su FatturaPro → incasso vero
    p_id = scenario(); FakeFP._next_existing = {"2026/00001609/SAK - Fattura"}
    r = _sync(monkeypatch, test_db_session, [row], notif={"D": ["RicevutaConsegna"]})
    p = test_db_session.query(Invoice).get(p_id)
    assert p.status == "paid" and p.paid_at is not None and p.source_id is None and r["voided"] == 0
    assert ("search", "documenti.NumeroSezionale", "00001609") in FakeFP.calls
    # 2) ricerca fallita → nessuna scrittura
    p_id = scenario(); FakeFP._next_search_ok = False
    r = _sync(monkeypatch, test_db_session, [row], notif={"D": ["RicevutaConsegna"]})
    p = test_db_session.query(Invoice).get(p_id)
    assert p.status == "paid" and p.source_id == "D" and r["voided"] == 0
    # 3) numero sparito → fantasma
    p_id = scenario()
    r = _sync(monkeypatch, test_db_session, [row], notif={"D": ["RicevutaConsegna"]})
    p = test_db_session.query(Invoice).get(p_id)
    assert p.status == "void" and p.paid_at is None and r["voided"] == 1


def test_phantom_paid_with_other_recipient_is_kept(monkeypatch, test_db_session):
    p_id = _mk(test_db_session, "1609", source_id="D", doc_id_verified=False, status="paid", amount=100.0, amount_due=0, days_overdue=0,
               paid_at=datetime(2026, 9, 14), customer_name_raw="ALFA SRL").id
    _mk(test_db_session, "1600", source_id="E", doc_id_verified=False, amount=100.0, amount_due=100.0, customer_name_raw="BETA SRL")
    r = _sync(monkeypatch, test_db_session, [_raw("1600", "D", "notified", balance=100.0, name="BETA SRL")], notif={"D": ["RicevutaConsegna"]})
    p = test_db_session.query(Invoice).get(p_id)
    assert p.status == "paid" and p.paid_at is not None and r["voided"] == 0


def test_fossil_twin_grace_is_two_absences(monkeypatch, test_db_session):
    """M1: gemella senza storia con streak 0 → al primo ciclo solo streak, al
    secondo annullata (nessun doppio incremento)."""
    l2 = _mk(test_db_session, "N2/2026", source_id="F", doc_id_verified=False, customer_name_raw="ACME SRL", amount=100.0, missing_streak=0).id
    _mk(test_db_session, "N1/2026", source_id="E", doc_id_verified=False, customer_name_raw="ACME SRL", amount=100.0)
    rows = [_raw("N1/2026", "F", "notified", balance=100.0, name="ACME SRL")]
    r1 = _sync(monkeypatch, test_db_session, rows, notif={"F": ["RicevutaConsegna"]})
    b = test_db_session.query(Invoice).get(l2)
    assert b.status == "open" and b.missing_streak == 1 and r1["voided"] == 0
    r2 = _sync(monkeypatch, test_db_session, rows)
    b = test_db_session.query(Invoice).get(l2)
    assert b.status == "void" and r2["voided"] == 1
