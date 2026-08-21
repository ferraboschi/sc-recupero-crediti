"""Test del motore di deduplica anagrafica (engine/merge.py)."""

from datetime import date

import pytest

from backend.database import (
    Customer,
    CustomerAcceptedName,
    Invoice,
    RecoveryAction,
    RecoveryCase,
)
from backend.engine.merge import (
    auto_merge_exact_piva,
    find_piva_clusters,
    merge_customers,
    names_correspond,
)

# P.IVA italiana checksum-valida (la usa is_checksum_backed per l'auto-merge).
PIVA_A = "12345670785"


def _cust(session, nome, piva=None, **kw):
    c = Customer(ragione_sociale=nome, partita_iva=piva, source=kw.pop("source", "shopify"), **kw)
    session.add(c)
    session.commit()
    return c


def _inv(session, customer, num, amount=100.0, **kw):
    i = Invoice(
        invoice_number=num,
        amount=amount,
        amount_due=amount,
        source_platform="fatturapro",
        customer_id=customer.id,
        status=kw.pop("status", "open"),
        **kw,
    )
    session.add(i)
    session.commit()
    return i


# ── names_correspond ────────────────────────────────────────────────

def test_names_correspond_subset_true():
    # Il caso Basara: sottoinsieme → corrisponde (token_set = 100).
    ok, score = names_correspond("Basara", "Basara a socio unico")
    assert ok is True
    assert score >= 80


def test_names_correspond_legal_form_variants_true():
    ok, _ = names_correspond("Wabi srls", "WABI S.R.L.S.")
    assert ok is True


def test_names_correspond_unrelated_false():
    # Stessa P.IVA ma nome-spazzatura estraneo: NON deve corrispondere.
    ok, score = names_correspond("ristorante", "Basara Milano Italia")
    assert ok is False
    assert score < 85


def test_names_correspond_generic_subset_false():
    # F1: 'ristorante' ⊆ 'Ristorante Da Gino' passa token_set (100) ma condivide
    # solo una parola di CATEGORIA (nessun brand) → NON corrisponde.
    ok, _ = names_correspond("ristorante", "Ristorante Da Gino")
    assert ok is False


def test_names_correspond_shared_brand_true():
    ok, _ = names_correspond("Basara", "Basara Milano Italia")
    assert ok is True


# ── merge_customers: fatture ────────────────────────────────────────

def test_merge_moves_invoices_and_marks_dup(test_db_session):
    s = test_db_session
    survivor = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    dup = _cust(s, "Tanoshi Group", PIVA_A, source="fatturapro")
    _inv(s, survivor, "1")
    _inv(s, survivor, "2")
    _inv(s, dup, "3")

    merge_customers(s, survivor, dup)
    s.commit()

    surv_inv = s.query(Invoice).filter(Invoice.customer_id == survivor.id).count()
    dup_inv = s.query(Invoice).filter(Invoice.customer_id == dup.id).count()
    assert surv_inv == 3
    assert dup_inv == 0
    assert dup.merged_into == survivor.id


def test_merge_enriches_survivor_gaps(test_db_session):
    s = test_db_session
    survivor = _cust(s, "TANOSHI", PIVA_A, phone=None, email=None)
    dup = _cust(s, "Tanoshi Group", PIVA_A, phone="+39333", email="t@x.it")

    merge_customers(s, survivor, dup)
    s.commit()
    assert survivor.phone == "+39333"
    assert survivor.email == "t@x.it"


def test_merge_keeps_survivor_excluded_state(test_db_session):
    # Niente OR silenzioso su 'excluded' (sposterebbe credito dentro/fuori dal
    # 'lavorabile'): la sopravvissuta tiene il PROPRIO stato.
    s = test_db_session
    survivor = _cust(s, "TANOSHI", PIVA_A, excluded=False)
    dup = _cust(s, "Tanoshi Group", PIVA_A, excluded=True)
    merge_customers(s, survivor, dup)
    s.commit()
    assert survivor.excluded is False


def test_merge_adopts_richer_name(test_db_session):
    # F2: se il nome del survivor è un sottoinsieme stretto di quello del dup,
    # adotta il più informativo (evita di "intronizzare" un nome povero).
    s = test_db_session
    survivor = _cust(s, "Basara", PIVA_A, shopify_id="gid://1")
    dup = _cust(s, "Basara Milano Italia Srl", PIVA_A)
    merge_customers(s, survivor, dup)
    s.commit()
    assert survivor.ragione_sociale == "Basara Milano Italia Srl"


def test_merge_multiple_dups_each_open_case_no_integrity_error(test_db_session):
    # B: merge manuale di PIÙ duplicati (loop + un commit) con survivor senza
    # pratica aperta e due dup ciascuno con una aperta → una sola aperta alla
    # fine, nessun IntegrityError (flush deterministico).
    s = test_db_session
    survivor = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    d1 = _cust(s, "Tanoshi Group", PIVA_A)
    d2 = _cust(s, "Tanoshi Grp", PIVA_A)
    s.add_all([
        RecoveryCase(customer_id=d1.id, status="open"),
        RecoveryCase(customer_id=d2.id, status="open"),
    ])
    s.commit()
    merge_customers(s, survivor, d1)
    merge_customers(s, survivor, d2)
    s.commit()  # non deve violare uq_open_case_per_customer
    open_cases = s.query(RecoveryCase).filter(
        RecoveryCase.customer_id == survivor.id, RecoveryCase.status == "open"
    ).count()
    assert open_cases == 1


# ── merge_customers: pratiche (una sola aperta per cliente) ──────────

def test_merge_two_open_cases_keeps_one_open(test_db_session):
    s = test_db_session
    survivor = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    dup = _cust(s, "Tanoshi Group", PIVA_A)
    sc = RecoveryCase(customer_id=survivor.id, status="open", inherited_contacts=1)
    dc = RecoveryCase(customer_id=dup.id, status="open", inherited_contacts=2)
    s.add_all([sc, dc])
    s.commit()
    act = RecoveryAction(customer_id=dup.id, case_id=dc.id, action_type="first_contact")
    inv = _inv(s, dup, "9")
    inv.case_id = dc.id
    s.add(act)
    s.commit()

    merge_customers(s, survivor, dup)
    s.commit()  # non deve violare uq_open_case_per_customer

    open_cases = (
        s.query(RecoveryCase)
        .filter(RecoveryCase.customer_id == survivor.id, RecoveryCase.status == "open")
        .all()
    )
    assert len(open_cases) == 1
    assert open_cases[0].id == sc.id
    # Il tono eredita il max dei contatti.
    assert open_cases[0].inherited_contacts == 2
    # La pratica del dup è chiusa 'merged', azioni+fatture ripuntate.
    s.refresh(dc)
    assert dc.status == "closed"
    assert dc.closed_reason == "merged"
    assert s.query(RecoveryAction).filter(RecoveryAction.case_id == sc.id).count() == 1
    assert s.query(Invoice).filter(Invoice.case_id == sc.id).count() == 1


def test_merge_adopts_dup_open_case_when_survivor_has_none(test_db_session):
    s = test_db_session
    survivor = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    dup = _cust(s, "Tanoshi Group", PIVA_A)
    dc = RecoveryCase(customer_id=dup.id, status="open")
    s.add(dc)
    s.commit()

    merge_customers(s, survivor, dup)
    s.commit()
    s.refresh(dc)
    assert dc.customer_id == survivor.id
    assert dc.status == "open"


def test_merge_accepted_names_moved_and_deduped(test_db_session):
    s = test_db_session
    survivor = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    dup = _cust(s, "Tanoshi Group", PIVA_A)
    s.add_all([
        CustomerAcceptedName(customer_id=survivor.id, name_normalized="tanoshi"),
        CustomerAcceptedName(customer_id=dup.id, name_normalized="tanoshi"),  # dup collide
        CustomerAcceptedName(customer_id=dup.id, name_normalized="tanoshi milano"),
    ])
    s.commit()
    merge_customers(s, survivor, dup)
    s.commit()
    names = {
        n.name_normalized
        for n in s.query(CustomerAcceptedName).filter(
            CustomerAcceptedName.customer_id == survivor.id
        )
    }
    # nessun errore UNIQUE; il collidente è scartato, l'altro spostato.
    assert "tanoshi" in names
    assert "tanoshi milano" in names


# ── auto_merge_exact_piva ───────────────────────────────────────────

def test_auto_merge_corresponding_names(test_db_session):
    s = test_db_session
    a = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    _cust(s, "Tanoshi Group", PIVA_A, source="fatturapro")
    _inv(s, a, "1")

    res = auto_merge_exact_piva(s)
    assert res["merged"] == 1
    assert res["clusters_touched"] == 1
    active = s.query(Customer).filter(Customer.merged_into.is_(None)).count()
    assert active == 1


def test_auto_merge_leaves_noncorresponding_for_review(test_db_session):
    s = test_db_session
    _cust(s, "Basara Milano Italia", PIVA_A, shopify_id="gid://1")
    _cust(s, "ristorante", PIVA_A, source="fatturapro")  # nome estraneo

    res = auto_merge_exact_piva(s)
    assert res["merged"] == 0
    assert res["left_for_review"] == 1
    # Entrambe restano attive: decide l'operatore.
    assert s.query(Customer).filter(Customer.merged_into.is_(None)).count() == 2


def test_auto_merge_skips_foreign_piva(test_db_session):
    s = test_db_session
    _cust(s, "QOQA SA", "CHE112640276", shopify_id="gid://1")
    _cust(s, "QOQA SA", "CHE112640276", source="fatturapro")

    res = auto_merge_exact_piva(s)
    # Estera format-valida ma senza checksum: mai auto, solo lista.
    assert res["merged"] == 0
    assert res["left_for_review"] == 1


def test_auto_merge_generic_name_not_merged(test_db_session):
    # F1 (regola owner): 'ristorante' con la stessa P.IVA di 'Ristorante Sushi
    # Milano' passa token_set (subset=100) ma è solo categoria → NON auto,
    # va in revisione.
    s = test_db_session
    _cust(s, "Ristorante Sushi Milano", PIVA_A, shopify_id="gid://1")
    _cust(s, "ristorante", PIVA_A, source="fatturapro")
    res = auto_merge_exact_piva(s)
    assert res["merged"] == 0
    assert res["left_for_review"] == 1
    assert s.query(Customer).filter(Customer.merged_into.is_(None)).count() == 2


def test_auto_merge_shared_brand_merged(test_db_session):
    s = test_db_session
    a = _cust(s, "Basara Milano Italia", PIVA_A, shopify_id="gid://1")
    _cust(s, "Basara", PIVA_A, source="fatturapro")
    _inv(s, a, "1")
    res = auto_merge_exact_piva(s)
    assert res["merged"] == 1


def test_auto_merge_skips_mixed_excluded(test_db_session):
    # A: cluster con esclusione mista → mai auto (policy sul denaro), in lista.
    s = test_db_session
    _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1", excluded=True)
    _cust(s, "Tanoshi Group", PIVA_A, source="fatturapro", excluded=False)
    res = auto_merge_exact_piva(s)
    assert res["merged"] == 0
    assert res["left_for_review"] == 1
    assert s.query(Customer).filter(Customer.merged_into.is_(None)).count() == 2


def test_pick_survivor_prefers_active_over_excluded(test_db_session):
    # La sopravvissuta non deve essere l'esclusa (nasconderebbe credito),
    # anche se l'esclusa ha lo shopify_id.
    s = test_db_session
    from backend.engine.merge import _pick_survivor
    excl = _cust(s, "Tanoshi", PIVA_A, shopify_id="gid://1", excluded=True)
    active = _cust(s, "Tanoshi", PIVA_A, source="fatturapro", excluded=False)
    survivor = _pick_survivor([excl, active], {})
    assert survivor.id == active.id


def test_find_clusters_excludes_already_merged(test_db_session):
    s = test_db_session
    survivor = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    dup = _cust(s, "Tanoshi Group", PIVA_A)
    merge_customers(s, survivor, dup)
    s.commit()
    clusters = find_piva_clusters(s)
    # dup ha merged_into → non forma più cluster.
    assert clusters == {}


# ── Endpoint API ────────────────────────────────────────────────────

def test_merge_suggestions_endpoint(test_client, test_db_session):
    s = test_db_session
    survivor = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    _cust(s, "Tanoshi Group", PIVA_A, source="fatturapro")

    r = test_client.get("/api/customers/merge-suggestions")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    cl = data["clusters"][0]
    assert cl["survivor_id"] == survivor.id
    assert cl["auto_eligible"] is True
    assert len(cl["members"]) == 2


def test_merge_endpoint_merges_same_piva(test_client, test_db_session):
    s = test_db_session
    survivor = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    dup = _cust(s, "ristorante", PIVA_A, source="fatturapro")  # nome estraneo: merge MANUALE
    _inv(s, dup, "1")

    r = test_client.post(
        "/api/customers/merge",
        json={"survivor_id": survivor.id, "duplicate_ids": [dup.id]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["merged"] == 1
    s.refresh(dup)
    assert dup.merged_into == survivor.id


def test_merge_endpoint_rejects_piva_mismatch(test_client, test_db_session):
    s = test_db_session
    survivor = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    other = _cust(s, "Altra azienda", "12345670786")  # P.IVA diversa

    r = test_client.post(
        "/api/customers/merge",
        json={"survivor_id": survivor.id, "duplicate_ids": [other.id]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["merged"] == 0
    assert body["skipped"][0]["reason"] == "piva_mismatch"
    s.refresh(other)
    assert other.merged_into is None


def test_customer_list_excludes_merged(test_client, test_db_session):
    s = test_db_session
    survivor = _cust(s, "TANOSHI GROUP SRLS", PIVA_A, shopify_id="gid://1")
    dup = _cust(s, "Tanoshi Group", PIVA_A)
    merge_customers(s, survivor, dup)
    s.commit()

    r = test_client.get("/api/customers?limit=100")
    assert r.status_code == 200
    ids = {it["id"] for it in r.json()["items"]}
    assert survivor.id in ids
    assert dup.id not in ids
