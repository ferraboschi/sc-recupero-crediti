"""Test del fetch lista FatturaPro (batch 2026-07-15, 2026-07-17).

Il vecchio fetch parsava la pagina 1 renderizzata (ordinamento di default
del sito) e paginava le successive con orderby imposto: un mismatch di
ordinamento faceva saltare deterministicamente una finestra di fatture a
ogni sync (mai create nell'app — il caso Belfiore 655/2026). Ora TUTTE le
pagine arrivano dalla stessa query AJAX (start=0), le righe si deduplicano
per numero fattura e le righe-fattura scartate marcano il fetch PARTIAL.

2026-07-17 — la paginazione a offset è sparita dal percorso normale. Restava
un modo silenzioso di perdere fatture: `xcrud[orderby]` è `documenti.Data`,
che NON è univoca, e su un ordinamento non totale il DB è libero di
restituire i pari-data in un ordine diverso a ogni pagina. Una finestra che
scivola RIPETE righe su una pagina e ne SALTA altre; la deduplica nascondeva
le ripetizioni e dei salti non si accorgeva nessuno. Ora si chiede la lista
in UNA pagina (niente OFFSET = niente finestra che scivoli) e una sonda
DIMOSTRA che è tutta lì. Vedi TestDannoRigheMaiCreate / TestDannoFantasmiPaid.

2026-08-18 — INCIDENTE DI PRODUZIONE (secondo fix, quello giusto). In produzione
il server CLAMPA il limit (tronca `limit=5000` a ~100), quindi la pagina unica
non basta mai e si cade SEMPRE nel ripiego a offset. PR #14 aveva provato a
lasciare `xcrud[orderby]` VUOTO sperando che il default di xcrud fosse la PK
univoca: FALSO, verificato sul FatturaPro reale — la pagina `documenti.php?s=1`
è ordinata di default per `documenti.Data` (colonna "↓ Data"), NON univoca.
Con orderby vuoto la finestra offset scivolava ancora sui pari-data → partial
per sempre ("551 agg, 0 pagate (PARZIALE)"), payment detection mai eseguita,
scaduto solo in crescita. Il fix VERO: forzare `documenti.NumeroSezionale` (il
numero fattura progressivo), l'UNICA colonna univoca della lista documenti.
Su una chiave univoca e totale la finestra offset piastrella senza buchi anche
sotto clamp, e la pagina finale corta DIMOSTRA la completezza → partial=False.
La deduplica resta come rete: se NumeroSezionale non fosse univoco, un salto
forza un duplicato → `partial=True` (mai una fattura 'paid' per errore).
Vedi TestFixOrdinamentoStabileSbloccaSottoClamp.
"""

import random
from datetime import date, timedelta

from backend.connectors.fatturapro import FatturaProConnector
from backend.database import Invoice


def _row(number, name="Cliente SRL", total="100,00", balance="100,00", doc_id=None):
    doc = f'<a data-doc_id="{doc_id}" href="#">apri</a>' if doc_id else ""
    return (
        f"<tr><td>{number}</td><td>01/05/2026</td><td>{name}</td>"
        f"<td>{total}</td><td>{balance}</td>{doc}</tr>"
    )


def _page(rows, with_key=True, with_header=True):
    header = (
        "<tr><th>Documento</th><th>Data</th><th>Destinatario</th>"
        "<th>Totale</th><th>Saldo</th></tr>"
    ) if with_header else ""
    key = '<input type="hidden" name="key" value="k1">' if with_key else ""
    return f"<table>{header}{''.join(rows)}</table>{key}"


class FakeResponse:
    def __init__(self, html):
        self.text = html
        self.url = "https://cloud.fatturapro.click/documenti.php?s=1"

    def raise_for_status(self):
        return None


def _connector(monkeypatch, rendered_html, ajax_pages):
    """Connettore con GET (pagina renderizzata) e POST (frammenti) finti."""
    conn = FatturaProConnector()
    conn._authenticated = True
    posts = {"calls": []}

    monkeypatch.setattr(
        conn.client, "get", lambda *a, **kw: FakeResponse(rendered_html)
    )

    def fake_post(url, data=None, **kw):
        posts["calls"].append(dict(data))
        idx = len(posts["calls"]) - 1
        html = ajax_pages[idx] if idx < len(ajax_pages) else _page([], with_key=False, with_header=False)
        return FakeResponse(html)

    monkeypatch.setattr(conn.client, "post", fake_post)
    return conn, posts


class TestUniformAjaxPagination:
    def test_all_rows_fetched_via_ajax_from_start_zero(self, monkeypatch):
        # La pagina renderizzata contiene FT-R (ordinamento del sito) che
        # NON deve essere usata come dati: i dati veri arrivano dall'AJAX,
        # in una pagina sola, e la sonda vuota chiude il discorso.
        rendered = _page([_row("FT-R")])
        page1 = _page([_row(f"FT-{i}") for i in range(12)], with_key=False, with_header=False)
        empty = _page([], with_key=False, with_header=False)
        conn, posts = _connector(monkeypatch, rendered, [page1, empty])

        invoices, partial = conn.fetch_overdue_invoices()

        assert partial is False
        numbers = [inv["invoice_number"] for inv in invoices]
        assert "FT-R" not in numbers  # righe renderizzate ignorate
        assert len(numbers) == 12
        # Una sola query per i dati: start=0, ordinamento su
        # documenti.NumeroSezionale (colonna UNIVOCA reale → a prova di clamp;
        # NON documenti.Data né orderby vuoto, che ereditava il default Data
        # non univoco), limite ampio
        assert posts["calls"][0]["xcrud[start]"] == "0"
        assert posts["calls"][0]["xcrud[orderby]"] == "documenti.NumeroSezionale"
        assert posts["calls"][0]["xcrud[limit]"] == "5000"
        # …e la sonda subito dopo l'ultima riga letta
        assert posts["calls"][1]["xcrud[start]"] == "12"
        assert len(posts["calls"]) == 2

    def test_boundary_duplicates_deduplicated_and_flagged_partial(self, monkeypatch):
        """Un duplicato al confine di pagina resta deduplicato — la lista non
        si corrompe — ma non passa più per innocuo.

        Questo test asseriva `partial is False`: era il difetto, scritto come
        specifica. Se la finestra ripete una riga ne sta saltando un'altra, in
        egual numero, e quelle saltate non compaiono in NESSUNA pagina: il
        fetch NON è completo. Oggi il confine esiste solo nel ripiego (il
        server ha troncato la pagina unica), e lì il duplicato è la prova che
        la lista si è mossa → PARZIALE → niente chiusure, niente 'paid'.
        """
        rendered = _page([_row("FT-R")])
        # Il server tronca a 10 righe qualunque limite gli si chieda
        page1 = _page([_row(f"FT-{i}") for i in range(10)], with_key=False, with_header=False)
        probe = _page([_row("FT-10")], with_key=False, with_header=False)
        # Ripiego: FT-9 ricompare (la finestra è scivolata)
        page2 = _page([_row("FT-9"), _row("FT-10")], with_key=False, with_header=False)
        conn, _ = _connector(monkeypatch, rendered, [page1, probe, page2])

        invoices, partial = conn.fetch_overdue_invoices()

        numbers = [inv["invoice_number"] for inv in invoices]
        assert numbers.count("FT-9") == 1  # deduplicata: la lista è pulita
        assert len(numbers) == 11
        assert partial is True  # ma non è completa, e ora lo dice

    def test_dropped_invoice_row_marks_partial(self, monkeypatch):
        """Una riga CON doc_id ma senza le celle attese è una fattura vera
        persa nel parsing: il fetch non può passare per completo (la
        payment detection marcherebbe pagate le fatture perse)."""
        rendered = _page([_row("FT-R")])
        broken_row = '<tr><td>FT-BROKEN</td><a data-doc_id="99" href="#">x</a></tr>'
        page1 = _page(
            [_row("FT-0"), broken_row], with_key=False, with_header=False
        )
        conn, _ = _connector(monkeypatch, rendered, [page1])

        invoices, partial = conn.fetch_overdue_invoices()

        assert partial is True
        assert [inv["invoice_number"] for inv in invoices] == ["FT-0"]

    def test_no_key_falls_back_to_rendered_page(self, monkeypatch):
        rendered = _page([_row("FT-R")], with_key=False)
        conn, posts = _connector(monkeypatch, rendered, [])

        invoices, partial = conn.fetch_overdue_invoices()

        assert [inv["invoice_number"] for inv in invoices] == ["FT-R"]
        assert partial is False  # meno di una pagina piena: lista completa
        assert posts["calls"] == []  # nessun AJAX possibile senza chiave

    def test_no_key_with_full_page_is_partial(self, monkeypatch):
        rendered = _page([_row(f"FT-{i}") for i in range(10)], with_key=False)
        conn, _ = _connector(monkeypatch, rendered, [])
        invoices, partial = conn.fetch_overdue_invoices()
        assert partial is True
        assert len(invoices) == 10

    def test_login_page_mid_pagination_marks_partial(self, monkeypatch):
        """Sessione scaduta a metà: il POST xcrud restituisce la pagina di
        login (200, zero righe). Non è la fine naturale della lista — il
        fetch deve risultare PARTIAL o la payment detection marcherebbe
        pagate tutte le fatture oltre il punto di rottura."""
        rendered = _page([_row("FT-R")])
        page1 = _page([_row(f"FT-{i}") for i in range(10)], with_key=False, with_header=False)
        login_page = (
            '<html><body><h1>Accesso alla piattaforma</h1>'
            '<input type="password" name="pwd"></body></html>'
        )
        conn, _ = _connector(monkeypatch, rendered, [page1, login_page])

        invoices, partial = conn.fetch_overdue_invoices()

        assert partial is True
        assert len(invoices) == 10

    def test_empty_number_row_with_doc_id_counts_as_drop(self, monkeypatch):
        rendered = _page([_row("FT-R")])
        ghost_row = (
            '<tr><td></td><td>01/05/2026</td><td>Cliente</td>'
            '<td>10,00</td><td>10,00</td>'
            '<td><a data-doc_id="77" href="#">x</a></td></tr>'
        )
        page1 = _page([_row("FT-0"), ghost_row], with_key=False, with_header=False)
        conn, _ = _connector(monkeypatch, rendered, [page1])

        invoices, partial = conn.fetch_overdue_invoices()

        assert partial is True
        assert [inv["invoice_number"] for inv in invoices] == ["FT-0"]


# ── Il DB con ordinamento NON totale ─────────────────────────────────
#
# `ORDER BY documenti.Data` + LIMIT/OFFSET: la data non è univoca, e MySQL
# è esplicito sul fatto che l'ordine dei pari è libero e può cambiare col
# piano di esecuzione — che dipende da LIMIT+OFFSET (priority queue del
# filesort). Qui la permutazione dei pari-data è funzione dell'offset:
# è il modello peggiore, e serve a dimostrare che il codice non ha DIFESE
# (non che il gestionale vero scivoli con questa intensità).


class UnstableXcrudBackend:
    """Serve rows[start:start+limit] permutando i pari-data a ogni query."""

    def __init__(self, rows, clamp=None):
        self.rows = rows          # lista viva: i test la mutano
        self.clamp = clamp        # server che tronca il limit senza dirlo
        self.calls = []

    def query(self, start, limit):
        self.calls.append((start, limit))
        if self.clamp is not None:
            limit = min(limit, self.clamp)
        buckets = {}
        for r in self.rows:
            buckets.setdefault(r["date"], []).append(r)
        ordered = []
        for d in sorted(buckets, reverse=True):
            group = list(buckets[d])
            random.Random(start * 100_000 + d.toordinal()).shuffle(group)
            ordered.extend(group)
        return ordered[start:start + limit]


def _invoices(n, per_day):
    """n fatture a gruppi di `per_day` con la stessa data (fatturazione a batch)."""
    return [
        {"number": f"2026/{i:08d}/SAK - Fattura",
         "date": date(2026, 5, 1) - timedelta(days=i // per_day)}
        for i in range(n)
    ]


def _dated_row(r):
    return (
        f'<tr><td>{r["number"]}</td><td>{r["date"].strftime("%d/%m/%Y")}</td>'
        f'<td>Cliente SRL</td><td>100,00</td><td>100,00</td>'
        f'<td><a data-doc_id="{r["number"]}" href="#">apri</a></td></tr>'
    )


def _unstable_connector(monkeypatch, backend):
    """Connettore su un backend che risponde a start/limit veri."""
    conn = FatturaProConnector()
    conn._authenticated = True
    monkeypatch.setattr(conn.client, "get", lambda *a, **kw: FakeResponse(_page([])))

    def fake_post(url, data=None, **kw):
        rows = backend.query(int(data["xcrud[start]"]), int(data["xcrud[limit]"]))
        return FakeResponse(_page([_dated_row(r) for r in rows],
                                  with_key=False, with_header=False))

    monkeypatch.setattr(conn.client, "post", fake_post)
    return conn


class TestDannoRigheMaiCreate:
    """Danno #1 — il più insidioso: le fatture che non arrivano MAI.

    Una finestra che scivola salta righe che non compaiono in NESSUNA
    pagina. Il fetch si dichiara completo (nessuna riga è malformata:
    dropped_rows resta 0), quelle fatture non vengono mai create nell'app,
    e non le vede nessuno — nemmeno l'audit, che confronta ciò che c'è.
    È il caso Belfiore 655/2026.
    """

    def test_no_invoice_is_lost_between_pages(self, monkeypatch):
        backend = UnstableXcrudBackend(_invoices(434, per_day=8))
        conn = _unstable_connector(monkeypatch, backend)

        invoices, partial = conn.fetch_overdue_invoices()

        assert len(invoices) == 434, (
            f"{434 - len(invoices)} fatture perse fra le pagine — e il fetch "
            f"si dichiara partial={partial}"
        )
        assert partial is False

    def test_never_reports_a_truncated_list_as_complete(self, monkeypatch):
        """L'invariante non negoziabile: o la lista è intera, o è PARZIALE."""
        backend = UnstableXcrudBackend(_invoices(434, per_day=8))
        conn = _unstable_connector(monkeypatch, backend)

        invoices, partial = conn.fetch_overdue_invoices()

        assert len(invoices) == 434 or partial is True


class TestDannoFantasmiPaid:
    """Danno #2 — le fatture vere marcate 'paid'.

    A dati IMMUTATI la perdita si ripete identica: le righe perse non sono
    mai state create, quindi non diventano 'paid'. Il fantasma nasce quando
    la lista CAMBIA (una fattura pagata davvero esce): gli offset slittano,
    l'insieme perso si ridisegna e ci finisce dentro una fattura GIÀ in DB.
    Poi basta un sync a dati fermi perché la seconda assenza consecutiva la
    chiuda. PAID_ABSENCE_STREAK non è inutile — è debole: filtra i cicli in
    cui i dati si muovono, non le coppie di sync a dati fermi (le notti, i
    weekend: la maggioranza dei sync orari).
    """

    def test_open_invoice_is_never_marked_paid(self, monkeypatch, test_db_session):
        from backend.api import sync as sync_mod

        backend = UnstableXcrudBackend(_invoices(434, per_day=8))
        conn = _unstable_connector(monkeypatch, backend)

        class _FakeFP:
            def login(self):
                return True

            def fetch_overdue_invoices(self):
                return conn.fetch_overdue_invoices()

            def fetch_scadenze_map(self, **kw):
                return {}, True

            def fetch_clienti_map(self):
                return {}, True

            def close(self):
                pass

        monkeypatch.setattr(sync_mod, "FatturaProConnector", lambda *a, **kw: _FakeFP())
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: test_db_session)

        sync_mod._sync_invoices_task()      # #1 popola l'app
        backend.rows.pop(3)                 # una fattura pagata DAVVERO esce
        sync_mod._sync_invoices_task()      # #2 la lista si muove
        sync_mod._sync_invoices_task()      # #3 dati fermi → seconda assenza

        live = {r["number"] for r in backend.rows}
        ghosts = [
            inv.invoice_number
            for inv in test_db_session.query(Invoice).filter(Invoice.status == "paid").all()
            if inv.invoice_number in live
        ]
        assert ghosts == [], (
            f"{len(ghosts)} fatture che il gestionale elenca ANCORA da "
            f"incassare sono state marcate 'paid' con amount_due=0"
        )


class TestSondaDiCompletezza:
    """La pagina unica non basta da sola: una pagina più corta del limite
    chiesto NON dimostra la fine della lista — il server potrebbe averla
    troncata in silenzio. La sonda chiede la riga successiva."""

    def test_silent_truncation_is_unmasked(self, monkeypatch):
        """Server che tronca a 100 righe qualunque limit gli si chieda.

        Senza sonda: 100 righe dichiarate COMPLETE e 334 fatture svanite.
        Con la sonda: il troncamento emerge e le righe si raccolgono lo
        stesso, ripiegando sulla paginazione — perché una lista dichiarata
        parziale blocca chiusure e 'paid', ma le fatture vanno CREATE
        comunque, o si ricade nel danno #1 (invisibili).
        """
        rows = [{"number": f"2026/{i:08d}/SAK - Fattura",
                 "date": date(2026, 5, 1) - timedelta(days=i)}  # date tutte diverse
                for i in range(434)]
        backend = UnstableXcrudBackend(rows, clamp=100)
        conn = _unstable_connector(monkeypatch, backend)

        invoices, partial = conn.fetch_overdue_invoices()

        assert len(invoices) == 434, "il ripiego deve raccogliere tutte le righe"
        assert not (len(invoices) == 100 and partial is False), \
            "lista troncata dichiarata completa"
        # Ordinamento stabile (date tutte distinte): il ripiego DIMOSTRA la
        # completezza (ultima pagina corta, zero duplicati) → non c'è nulla
        # da dichiarare parziale. Un partial=True qui sarebbe una bugia
        # simmetrica, e bloccherebbe le chiusure per sempre.
        assert partial is False
        assert (0, 5000) in backend.calls, "prima una pagina sola"
        assert (100, 10) in backend.calls, "poi la sonda sulla riga successiva"

    def test_truncation_plus_unstable_order_is_partial(self, monkeypatch):
        """Troncamento E ordinamento instabile: il ripiego deve paginare, e
        lì la finestra può scivolare davvero. I duplicati sono la prova che
        sta saltando righe → PARZIALE. Uno stallo onesto è sempre meglio di
        una corruzione silenziosa."""
        backend = UnstableXcrudBackend(_invoices(434, per_day=8), clamp=100)
        conn = _unstable_connector(monkeypatch, backend)

        invoices, partial = conn.fetch_overdue_invoices()

        assert len(invoices) == 434 or partial is True

    def test_single_page_when_the_list_fits(self, monkeypatch):
        """Il caso normale: una richiesta + una sonda, e basta."""
        backend = UnstableXcrudBackend(_invoices(30, per_day=8))
        conn = _unstable_connector(monkeypatch, backend)

        invoices, partial = conn.fetch_overdue_invoices()

        assert len(invoices) == 30
        assert partial is False
        assert backend.calls == [(0, 5000), (30, 10)]


# ── Il fix dell'incidente di produzione ──────────────────────────────
#
# Il server REALE non è ostile: TRONCA (clampa) il limit — come fa con
# clienti.php, che `_paginate_xcrud_list` scarica comunque per intero (1251
# righe a pagine da 100). La chiave UNIVOCA della lista documenti è
# `documenti.NumeroSezionale` (il numero fattura progressivo); il default della
# pagina, invece, è `documenti.Data`, NON univoca. Questo backend modella
# ENTRAMBE le verità: clampa il limit e, SOLO su orderby=documenti.NumeroSezionale,
# serve una finestra offset STABILE (ordinata per numero documento). Su
# qualsiasi altro orderby — vuoto (che eredita il default Data) o
# documenti.Data — permuta i pari-data in funzione dell'offset, il modello che
# teneva partial=True. È questo che fa fallire il test senza il fix e passare
# col fix.


class ClampedDefaultOrderBackend:
    """Server che CLAMPA il limit. Ordina in modo STABILE (per chiave univoca)
    SOLO se l'orderby è `documenti.NumeroSezionale`; su vuoto/documenti.Data —
    non univoci — permuta i pari-data e la finestra scivola."""

    UNIQUE_ORDERBY = "documenti.NumeroSezionale"

    def __init__(self, rows, clamp=100):
        self.rows = rows
        self.clamp = clamp
        self.calls = []

    def query(self, start, limit, orderby):
        self.calls.append((start, limit, orderby))
        limit = min(limit, self.clamp)
        if orderby == self.UNIQUE_ORDERBY:
            # Colonna univoca reale → ordine TOTALE e stabile fra query.
            ordered = sorted(self.rows, key=lambda r: r["number"])
        else:
            # Orderby non univoco (vuoto = default Data, o documenti.Data
            # esplicito): permuta i pari-data in funzione dell'offset — il
            # modello che teneva partial=True.
            buckets = {}
            for r in self.rows:
                buckets.setdefault(r["date"], []).append(r)
            ordered = []
            for d in sorted(buckets, reverse=True):
                group = list(buckets[d])
                random.Random(start * 100_003 + d.toordinal()).shuffle(group)
                ordered.extend(group)
        return ordered[start:start + limit]


def _default_order_connector(monkeypatch, backend):
    """Connettore il cui fake_post passa anche l'orderby al backend."""
    conn = FatturaProConnector()
    conn._authenticated = True
    monkeypatch.setattr(conn.client, "get", lambda *a, **kw: FakeResponse(_page([])))

    def fake_post(url, data=None, **kw):
        rows = backend.query(
            int(data["xcrud[start]"]),
            int(data["xcrud[limit]"]),
            data.get("xcrud[orderby]", ""),
        )
        return FakeResponse(_page([_dated_row(r) for r in rows],
                                  with_key=False, with_header=False))

    monkeypatch.setattr(conn.client, "post", fake_post)
    return conn


class TestFixOrdinamentoStabileSbloccaSottoClamp:
    """Il fix: il server clampa il limit (qui a 100) su ~770 fatture con DATE
    RIPETUTE (fatturazione a batch) — lo scenario che teneva partial=True a
    ogni sync. Ordinando su `documenti.NumeroSezionale` (colonna univoca reale)
    la finestra offset piastrella senza scivolare: TUTTE le righe arrivano e la
    pagina finale corta DIMOSTRA la completezza → partial=False, e il
    rilevamento pagamenti riparte."""

    def test_clamp_100_su_770_righe_date_ripetute_e_partial_false(self, monkeypatch):
        rows = _invoices(770, per_day=8)  # 770 fatture, ~8 per data → date NON univoche
        backend = ClampedDefaultOrderBackend(rows, clamp=100)
        conn = _default_order_connector(monkeypatch, backend)

        invoices, partial = conn.fetch_overdue_invoices()

        assert len(invoices) == 770, (
            f"il clamp a 100 non deve far perdere fatture: raccolte "
            f"{len(invoices)}/770"
        )
        nums = {inv["invoice_number"] for inv in invoices}
        assert len(nums) == 770  # tutte distinte, nessuna persa né ripetuta
        assert partial is False, (
            "ordinamento stabile + pagina finale corta = completezza dimostrata"
        )

    def test_la_lista_fatture_pagina_su_numero_sezionale(self, monkeypatch):
        """Regressione sul fix stesso: la lista fatture deve paginare su
        `documenti.NumeroSezionale` (colonna univoca) — NON su documenti.Data
        e NON con orderby vuoto (che eredita il default Data, non univoco)."""
        backend = ClampedDefaultOrderBackend(_invoices(300, per_day=8), clamp=100)
        conn = _default_order_connector(monkeypatch, backend)

        conn.fetch_overdue_invoices()

        assert backend.calls, "nessuna query effettuata"
        assert all(
            orderby == "documenti.NumeroSezionale"
            for (_s, _l, orderby) in backend.calls
        ), (
            "la lista fatture deve paginare su documenti.NumeroSezionale "
            "(colonna univoca), non su documenti.Data né orderby vuoto"
        )

    def test_numero_sezionale_non_univoco_degrada_a_partial_non_a_false_paid(self, monkeypatch):
        """La rete di sicurezza: SE — contro l'atteso — `documenti.NumeroSezionale`
        NON fosse univoco, la paginazione scivolerebbe. Il duplicato lo rileva
        → partial=True. Mai una lista mozza dichiarata completa (che
        marcherebbe 'paid' le fatture non lette)."""
        # UnstableXcrudBackend ignora l'orderby e permuta SEMPRE i pari-data
        # (per_day=8 + clamp): modella l'ipotesi pessimista in cui la colonna
        # scelta non fosse davvero univoca.
        backend = UnstableXcrudBackend(_invoices(434, per_day=8), clamp=100)
        conn = _unstable_connector(monkeypatch, backend)

        invoices, partial = conn.fetch_overdue_invoices()

        assert len(invoices) == 434 or partial is True

    def test_end_to_end_il_rilevamento_pagamenti_riparte(self, monkeypatch, test_db_session):
        """La prova che l'incidente è chiuso, dal fetch al DB. Sotto clamp +
        ordinamento stabile il fetch è COMPLETO (partial=False), quindi la
        payment detection RIPARTE: una fattura che esce davvero dalla lista
        viene marcata 'paid' dopo due assenze, mentre tutte le altre restano
        'open' (nessun false-paid)."""
        from backend.api import sync as sync_mod

        backend = ClampedDefaultOrderBackend(_invoices(250, per_day=8), clamp=100)
        conn = _default_order_connector(monkeypatch, backend)

        class _FakeFP:
            def login(self):
                return True

            def fetch_overdue_invoices(self):
                return conn.fetch_overdue_invoices()

            def fetch_scadenze_map(self, **kw):
                return {}, True

            def fetch_clienti_map(self):
                return {}, True

            def close(self):
                pass

        monkeypatch.setattr(sync_mod, "FatturaProConnector", lambda *a, **kw: _FakeFP())
        monkeypatch.setattr(sync_mod, "get_session_direct", lambda: test_db_session)

        sync_mod._sync_invoices_task()      # #1 popola (250 fatture, fetch completo)
        paid_number = backend.rows[3]["number"]
        backend.rows.pop(3)                 # una fattura pagata DAVVERO esce
        sync_mod._sync_invoices_task()      # #2 assente (streak 1)
        sync_mod._sync_invoices_task()      # #3 assente (streak 2) → paid

        paid = {
            inv.invoice_number
            for inv in test_db_session.query(Invoice)
            .filter(Invoice.status == "paid").all()
        }
        # La fattura uscita è stata rilevata come pagata: il freeze è superato.
        assert paid_number in paid
        # Nessuna fattura ancora in lista è stata marcata 'paid' per errore.
        live = {r["number"] for r in backend.rows}
        assert not (paid & live), "false-paid su fatture ancora da incassare"
