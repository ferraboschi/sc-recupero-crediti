"""Il campo `agents` (agente di vendita dai tag cliente Shopify) accompagna la
ragione sociale in OGNI lista dell'app: posizioni, clienti, ricerca, attività,
promemoria, calendario, avvocato, bonifica, duplicati, assegni, audit sistema.

Regole owner 9/11/2026 (backend/engine/agents.py): Maggioni / Izzo / Scotti;
Rendina → Izzo; Sgariglia De Leo → Maggioni; più agenti = tutti.
"""
from datetime import date, datetime, timedelta

from backend.database import Customer, Invoice, RecoveryCase, RecoveryAction
from tests.test_avvocato import _cust, _inv, _contact, _make_candidate


def _agent_cust(s, nome="Shabu Casatenovo", tags="B2B, Maggioni", **kw):
    kw.setdefault("recovery_status", "idle")
    return _cust(s, nome, tags=tags, **kw)


# ── Clienti ──────────────────────────────────────────────────────────

class TestCustomers:
    def test_list_exposes_agents_per_customer(self, test_client, test_db_session):
        a = _agent_cust(test_db_session, "Shabu Casatenovo", "B2B, SCOTTI")
        b = _agent_cust(test_db_session, "Senza Agente Srl", "B2B, scp")
        c = _agent_cust(test_db_session, "Nessun Tag Srl", None)
        for cust in (a, b, c):
            _inv(test_db_session, cust, f"{cust.id}/1", 500.0)
        r = test_client.get("/api/customers")
        assert r.status_code == 200
        by_id = {i["id"]: i for i in r.json()["items"]}
        assert by_id[a.id]["agents"] == ["Scotti"]
        assert by_id[b.id]["agents"] == []
        assert by_id[c.id]["agents"] == []

    def test_detail_applies_owner_aliases(self, test_client, test_db_session):
        c = _agent_cust(test_db_session, "Hamasei", "B2B, rendina")
        r = test_client.get(f"/api/customers/{c.id}")
        assert r.status_code == 200
        assert r.json()["agents"] == ["Izzo"]
        assert r.json()["tags"] == "B2B, rendina"  # il tag grezzo resta visibile

    def test_detail_keeps_every_agent(self, test_client, test_db_session):
        c = _agent_cust(test_db_session, "Doppio Srl", "Scotti, B2B, Maggioni")
        r = test_client.get(f"/api/customers/{c.id}")
        assert r.json()["agents"] == ["Maggioni", "Scotti"]

    def test_suggest_exposes_agents(self, test_client, test_db_session):
        _agent_cust(test_db_session, "Domò Milano", "B2B, sgariglia de leo")
        r = test_client.get("/api/customers/suggest", params={"q": "Domo Milano"})
        assert r.status_code == 200
        items = r.json()["items"]
        assert items and items[0]["ragione_sociale"] == "Domò Milano"
        assert items[0]["agents"] == ["Maggioni"]

    def test_bonifica_suggestions_expose_agents(self, test_client, test_db_session):
        c = _agent_cust(test_db_session, "Senza Piva Srl", "B2B, Izzo", partita_iva=None)
        inv = _inv(test_db_session, c, "SP/1", 800.0)
        inv.customer_piva_raw = "12345670785"
        test_db_session.commit()
        r = test_client.get("/api/customers/bonifica-suggestions")
        assert r.status_code == 200
        item = next(i for i in r.json()["items"] if i["customer_id"] == c.id)
        assert item["agents"] == ["Izzo"]

    def test_merge_suggestions_expose_agents_per_member(self, test_client, test_db_session):
        a = _agent_cust(test_db_session, "Zuma Rome", "B2B, Izzo", partita_iva="12345670785")
        b = _agent_cust(test_db_session, "Zuma Rome Srl", "B2B, Maggioni", partita_iva="12345670785")
        r = test_client.get("/api/customers/merge-suggestions")
        assert r.status_code == 200
        clusters = r.json()["clusters"]
        assert clusters, "cluster atteso per la P.IVA condivisa"
        members = {m["id"]: m for m in clusters[0]["members"]}
        assert members[a.id]["agents"] == ["Izzo"]
        assert members[b.id]["agents"] == ["Maggioni"]


# ── Posizioni ────────────────────────────────────────────────────────

class TestPositions:
    def test_list_and_detail_expose_customer_agents(self, test_client, test_db_session):
        c = _agent_cust(test_db_session, "Kiyo Ristorante", "B2B, Maggioni")
        inv = _inv(test_db_session, c, "K/1", 1200.0)
        r = test_client.get("/api/positions")
        assert r.status_code == 200
        item = next(i for i in r.json()["items"] if i["id"] == inv.id)
        assert item["customer"]["agents"] == ["Maggioni"]
        d = test_client.get(f"/api/positions/{inv.id}")
        assert d.status_code == 200
        assert d.json()["customer"]["agents"] == ["Maggioni"]

    def test_unassigned_position_has_no_customer_block(self, test_client, test_db_session):
        inv = Invoice(
            invoice_number="ORF/1", amount=100.0, amount_due=100.0,
            issue_date=date.today() - timedelta(days=60),
            due_date=date.today() - timedelta(days=30), days_overdue=30,
            customer_id=None, source_platform="fatturapro", status="open",
            customer_name_raw="Sconosciuto Srl",
        )
        test_db_session.add(inv)
        test_db_session.commit()
        r = test_client.get("/api/positions")
        item = next(i for i in r.json()["items"] if i["id"] == inv.id)
        assert item["customer"] is None

    def test_suggestions_expose_suggested_customer_agents(self, test_client, test_db_session):
        c = _agent_cust(test_db_session, "Fresco Cocktail Shop", "B2B, SCOTTI")
        inv = Invoice(
            invoice_number="SUG/1", amount=300.0, amount_due=300.0,
            issue_date=date.today() - timedelta(days=60),
            due_date=date.today() - timedelta(days=30), days_overdue=30,
            customer_id=None, source_platform="fatturapro", status="open",
            customer_name_raw="Fresco Cocktail", suggested_customer_id=c.id,
            suggested_score=90, suggested_method="fuzzy",
        )
        test_db_session.add(inv)
        test_db_session.commit()
        r = test_client.get("/api/positions/suggestions")
        assert r.status_code == 200
        item = next(i for i in r.json()["items"] if i["id"] == inv.id)
        assert item["suggested_customer"]["agents"] == ["Scotti"]


# ── Cruscotto / Attività ─────────────────────────────────────────────

class TestDashboard:
    def test_search_exposes_agents(self, test_client, test_db_session):
        c = _agent_cust(test_db_session, "Hiromi Maison", "B2B, Izzo, scp")
        _inv(test_db_session, c, "HM/1", 400.0)
        r = test_client.get("/api/dashboard/search", params={"q": "Hiromi"})
        assert r.status_code == 200
        res = next(x for x in r.json()["results"] if x["id"] == c.id)
        assert res["agents"] == ["Izzo"]

    def test_todos_expose_agents_for_new_contacts(self, test_client, test_db_session):
        c = _agent_cust(test_db_session, "Nozomi", "B2B, Maggioni")
        _inv(test_db_session, c, "NZ/1", 900.0, days_overdue=40)
        r = test_client.get("/api/dashboard/todos")
        assert r.status_code == 200
        todos = [t for t in r.json()["todos"] if t["customer_id"] == c.id]
        assert todos, "il cliente idle con scaduto deve comparire fra i promemoria"
        assert all(t["agents"] == ["Maggioni"] for t in todos)

    def test_todos_and_calendar_expose_agents_for_scheduled_actions(self, test_client, test_db_session):
        c = _agent_cust(test_db_session, "Inno Duomo", "B2B, SCOTTI", recovery_status="first_contact")
        _inv(test_db_session, c, "ID/1", 700.0, days_overdue=40)
        case = RecoveryCase(customer_id=c.id, status="open")
        test_db_session.add(case)
        test_db_session.commit()
        today = date.today()
        a = RecoveryAction(
            customer_id=c.id, case_id=case.id, action_type="second_contact",
            scheduled_date=today, created_at=datetime.utcnow(),
        )
        test_db_session.add(a)
        test_db_session.commit()
        todos = test_client.get("/api/dashboard/todos").json()["todos"]
        mine = [t for t in todos if t["customer_id"] == c.id]
        assert mine and all(t["agents"] == ["Scotti"] for t in mine)
        cal = test_client.get(
            "/api/dashboard/calendar", params={"year": today.year, "month": today.month}
        )
        assert cal.status_code == 200
        day_items = cal.json()["days"][today.isoformat()]
        entry = next(x for x in day_items if x["customer_id"] == c.id)
        assert entry["agents"] == ["Scotti"]

    def test_attivita_contacted_and_incassati_expose_agents(self, test_client, test_db_session):
        # Contattato: stato pratica attivo + scaduto aperto.
        c = _agent_cust(test_db_session, "Sakelleria", "B2B, Izzo", recovery_status="first_contact")
        _inv(test_db_session, c, "SK/1", 650.0, days_overdue=30)
        case = RecoveryCase(customer_id=c.id, status="open")
        test_db_session.add(case)
        test_db_session.commit()
        _contact(test_db_session, c, case, "first_contact", days_ago=10)
        # Incassato: fattura emessa PRIMA del sollecito e pagata DOPO.
        paid = Invoice(
            invoice_number="SK/0", amount=300.0, amount_due=0.0,
            issue_date=date.today() - timedelta(days=90),
            due_date=date.today() - timedelta(days=60), days_overdue=0,
            customer_id=c.id, source_platform="fatturapro", status="paid",
            updated_at=datetime.utcnow(),
        )
        test_db_session.add(paid)
        test_db_session.commit()
        r = test_client.get("/api/dashboard/attivita")
        assert r.status_code == 200
        data = r.json()
        contacted = next(x for x in data["contacted"] if x["id"] == c.id)
        assert contacted["agents"] == ["Izzo"]
        incassati = next(x for x in data["incassati"] if x["id"] == c.id)
        assert incassati["agents"] == ["Izzo"]
        assert incassati["paid_count"] == 1  # gli indici row[0..6] non sono slittati

    def test_assegni_insoluti_expose_agents(self, test_client, test_db_session):
        c = _agent_cust(test_db_session, "Yapa", "B2B, SCOTTI")
        inv = _inv(test_db_session, c, "YP/1", 500.0, days_overdue=20)
        inv.bounced_at = datetime.utcnow()
        test_db_session.commit()
        r = test_client.get("/api/dashboard")
        assert r.status_code == 200
        items = r.json()["assegni"]["insoluti"]["items"]
        item = next(x for x in items if x["invoice_id"] == inv.id)
        assert item["ragione_sociale"] == "Yapa"
        assert item["agents"] == ["Scotti"]

    def test_assegni_insoluto_without_customer_has_empty_agents(self, test_client, test_db_session):
        inv = Invoice(
            invoice_number="ORF/2", amount=100.0, amount_due=100.0,
            issue_date=date.today() - timedelta(days=60),
            due_date=date.today() - timedelta(days=30), days_overdue=30,
            customer_id=None, source_platform="fatturapro", status="open",
            bounced_at=datetime.utcnow(),
        )
        test_db_session.add(inv)
        test_db_session.commit()
        items = test_client.get("/api/dashboard").json()["assegni"]["insoluti"]["items"]
        item = next(x for x in items if x["invoice_id"] == inv.id)
        assert item["agents"] == []


# ── Avvocato ─────────────────────────────────────────────────────────

class TestAvvocato:
    def test_candidates_expose_agents(self, test_client, test_db_session):
        cust = _make_candidate(test_db_session, "ACME SRL", debt=2000, solleciti=2, last_days_ago=20)
        cust.tags = "B2B, Rendina"
        test_db_session.commit()
        r = test_client.get("/api/avvocato/candidates")
        assert r.status_code == 200
        item = next(x for x in r.json()["items"] if x["id"] == cust.id)
        assert item["agents"] == ["Izzo"]


# ── Sistema: audit abbinamenti (raggruppato per cliente) ─────────────

class TestSystemMatchAudit:
    def test_groups_expose_agents(self, test_client, test_db_session, monkeypatch):
        from backend.api import system as system_mod
        monkeypatch.setattr(system_mod, "get_session_direct", lambda: test_db_session)
        c = _agent_cust(test_db_session, "Shabu Casatenovo", "B2B, Maggioni", partita_iva="12345670785")
        inv = _inv(test_db_session, c, "AU/1", 500.0)
        inv.customer_name_raw = "Totalmente Diverso Spa"  # nome dissimile → problema
        inv.customer_piva_raw = "12345670785"
        inv.match_method = "fuzzy"
        test_db_session.commit()
        r = test_client.get("/api/system/match-audit")
        assert r.status_code == 200
        groups = [g for g in r.json()["groups"] if g["customer_id"] == c.id]
        assert groups, "atteso un gruppo di problemi per il cliente"
        assert groups[0]["agents"] == ["Maggioni"]
