"""Test della pagina "Utilizzo": registro giorno-per-giorno del lavoro reale.

Sola lettura, nessuna mutazione. Serve a monitorare quanto la persona ha davvero
usato il sistema: quante AZIONI di recupero (solleciti registrati) ha eseguito e
su quanti ACCOUNT, giorno per giorno.

Un'azione = una RecoveryAction con un `channel` valorizzato (l'unico flusso che
scrive un channel e il "Copia Messaggio"/WhatsApp realmente inviato), non
annullata. La "data" e' il giorno di calendario ITALIANO di `created_at` (non il
giorno UTC nudo). L'endpoint espone due viste:

- `per_giorno`: {data, azioni, account (clienti DISTINTI)}, ordinato data desc.
- `eventi`: {data, customer_id, cliente, action_type, channel}, ordinato data
  desc poi cliente.
"""

from datetime import datetime, timedelta

from sqlalchemy import event

from backend.database import Customer, RecoveryAction


def _customer(session, ragione_sociale):
    c = Customer(ragione_sociale=ragione_sociale)
    session.add(c)
    session.commit()
    return c


def _sollecito(session, customer_id, created_at, channel="whatsapp_copy",
               action_type="first_contact", cancelled=False):
    """Crea una RecoveryAction che rappresenta un sollecito inviato."""
    a = RecoveryAction(
        customer_id=customer_id,
        action_type=action_type,
        channel=channel,
        outcome="contacted",
        completed_at=created_at,
        cancelled=cancelled,
        created_at=created_at,
    )
    session.add(a)
    session.commit()
    return a


def _count_selects(test_db_session, do_request):
    """Numero di SELECT emessi dall'engine durante do_request()."""
    statements = []
    engine = test_db_session.get_bind()

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        do_request()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    return len(statements)


class TestUtilizzoEventi:
    def test_lists_sollecito_with_customer_name_and_italian_date(
        self, test_client, test_db_session
    ):
        c = _customer(test_db_session, "Rooftop S.R.L.")
        # 14 gennaio 23:30 UTC -> in Italia (UTC+1 d'inverno) e' gia' il 15
        # gennaio 00:30: la "data" del sollecito deve essere il giorno
        # ITALIANO (2026-01-15), non il giorno UTC nudo (2026-01-14).
        _sollecito(test_db_session, c.id,
                   datetime(2026, 1, 14, 23, 30, 0),
                   channel="whatsapp_copy")

        resp = test_client.get("/api/recovery/utilizzo")
        assert resp.status_code == 200
        data = resp.json()

        assert len(data["eventi"]) == 1
        ev = data["eventi"][0]
        assert ev["data"] == "2026-01-15"  # giorno italiano, non UTC
        assert ev["customer_id"] == c.id
        assert ev["cliente"] == "Rooftop S.R.L."
        assert ev["action_type"] == "first_contact"
        assert ev["channel"] == "whatsapp_copy"

    def test_eventi_ordered_by_date_desc_then_customer(
        self, test_client, test_db_session
    ):
        c1 = _customer(test_db_session, "Zeta S.R.L.")
        c2 = _customer(test_db_session, "Alfa S.R.L.")
        c3 = _customer(test_db_session, "Beta S.R.L.")
        # Due clienti lo stesso giorno (recente), uno un giorno prima.
        _sollecito(test_db_session, c1.id, datetime(2026, 3, 10, 9, 0))
        _sollecito(test_db_session, c2.id, datetime(2026, 3, 10, 9, 0))
        _sollecito(test_db_session, c3.id, datetime(2026, 3, 9, 9, 0))

        data = test_client.get("/api/recovery/utilizzo").json()
        righe = [(e["data"], e["cliente"]) for e in data["eventi"]]
        # Data desc, poi cliente asc: Alfa e Zeta il 10 (Alfa prima), Beta il 9.
        assert righe == [
            ("2026-03-10", "Alfa S.R.L."),
            ("2026-03-10", "Zeta S.R.L."),
            ("2026-03-09", "Beta S.R.L."),
        ]


class TestUtilizzoPerGiorno:
    def test_counts_actions_and_distinct_accounts_per_day(
        self, test_client, test_db_session
    ):
        c1 = _customer(test_db_session, "Rooftop S.R.L.")
        c2 = _customer(test_db_session, "Cantina Bianchi")
        # Stesso cliente, stesso giorno, DUE solleciti -> 1 account, 2 azioni.
        _sollecito(test_db_session, c1.id, datetime(2026, 4, 5, 9, 0))
        _sollecito(test_db_session, c1.id, datetime(2026, 4, 5, 15, 0))
        # Un secondo cliente lo stesso giorno -> 2 account, 3 azioni.
        _sollecito(test_db_session, c2.id, datetime(2026, 4, 5, 11, 0))

        data = test_client.get("/api/recovery/utilizzo").json()
        assert len(data["per_giorno"]) == 1
        giorno = data["per_giorno"][0]
        assert giorno["data"] == "2026-04-05"
        assert giorno["azioni"] == 3       # solleciti totali del giorno
        assert giorno["account"] == 2      # clienti DISTINTI del giorno

    def test_per_giorno_ordered_by_date_desc(
        self, test_client, test_db_session
    ):
        c = _customer(test_db_session, "Rooftop S.R.L.")
        _sollecito(test_db_session, c.id, datetime(2026, 5, 1, 9, 0))
        _sollecito(test_db_session, c.id, datetime(2026, 5, 3, 9, 0))
        _sollecito(test_db_session, c.id, datetime(2026, 5, 2, 9, 0))

        data = test_client.get("/api/recovery/utilizzo").json()
        date_list = [g["data"] for g in data["per_giorno"]]
        assert date_list == ["2026-05-03", "2026-05-02", "2026-05-01"]


class TestUtilizzoEsclusioni:
    def test_excludes_cancelled(self, test_client, test_db_session):
        c = _customer(test_db_session, "Rooftop S.R.L.")
        _sollecito(test_db_session, c.id, datetime(2026, 6, 1, 9, 0),
                   cancelled=False)
        _sollecito(test_db_session, c.id, datetime(2026, 6, 1, 12, 0),
                   cancelled=True)  # annullato: non conta

        data = test_client.get("/api/recovery/utilizzo").json()
        assert len(data["eventi"]) == 1
        assert data["per_giorno"][0]["account"] == 1
        assert data["per_giorno"][0]["azioni"] == 1

    def test_excludes_actions_without_channel(self, test_client, test_db_session):
        # Un'azione senza channel (es. un passaggio di workflow 'wait') non e'
        # un sollecito realmente inviato: e' il segnale piu' debole e non conta.
        c = _customer(test_db_session, "Rooftop S.R.L.")
        _sollecito(test_db_session, c.id, datetime(2026, 2, 1, 10, 0),
                   channel="whatsapp_copy")
        _sollecito(test_db_session, c.id, datetime(2026, 2, 2, 10, 0),
                   channel=None, action_type="wait")

        data = test_client.get("/api/recovery/utilizzo").json()
        assert len(data["eventi"]) == 1
        assert data["eventi"][0]["channel"] == "whatsapp_copy"
        assert len(data["per_giorno"]) == 1


class TestUtilizzoEmpty:
    def test_empty_returns_empty_lists_not_500(
        self, test_client, test_db_session
    ):
        resp = test_client.get("/api/recovery/utilizzo")
        assert resp.status_code == 200
        data = resp.json()
        assert data["eventi"] == []
        assert data["per_giorno"] == []


class TestUtilizzoEfficiency:
    def test_constant_query_count_no_n_plus_1(
        self, test_client, test_db_session
    ):
        # Il conteggio delle query NON deve crescere col numero di clienti/
        # solleciti: una sola query aggregata con join al nome cliente.
        def _seed(n):
            base = datetime(2026, 7, 1, 9, 0)
            for i in range(n):
                cust = _customer(test_db_session, f"Cliente {i}")
                _sollecito(test_db_session, cust.id, base + timedelta(days=i))

        _seed(3)
        q_small = _count_selects(
            test_db_session,
            lambda: test_client.get("/api/recovery/utilizzo"),
        )

        _seed(7)  # ora 10 clienti / 10 solleciti
        q_large = _count_selects(
            test_db_session,
            lambda: test_client.get("/api/recovery/utilizzo"),
        )

        assert q_small == q_large, (
            f"Query count non costante: {q_small} vs {q_large} (N+1?)"
        )
        # E deve restare piccolo: una query dati (+ eventuali di setup).
        assert q_large <= 2
