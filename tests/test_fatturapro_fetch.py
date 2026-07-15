"""Test del fetch lista FatturaPro (batch 2026-07-15).

Il vecchio fetch parsava la pagina 1 renderizzata (ordinamento di default
del sito) e paginava le successive con orderby imposto: un mismatch di
ordinamento faceva saltare deterministicamente una finestra di fatture a
ogni sync (mai create nell'app — il caso Belfiore 655/2026). Ora TUTTE le
pagine arrivano dalla stessa query AJAX (start=0), le righe si deduplicano
per numero fattura e le righe-fattura scartate marcano il fetch PARTIAL.
"""

from backend.connectors.fatturapro import FatturaProConnector


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
    def test_all_pages_fetched_via_ajax_from_start_zero(self, monkeypatch):
        # La pagina renderizzata contiene FT-R (ordinamento del sito) che
        # NON deve essere usata come dati: i dati veri arrivano dall'AJAX.
        rendered = _page([_row("FT-R")])
        page1 = _page([_row(f"FT-{i}") for i in range(10)], with_key=False, with_header=False)
        page2 = _page([_row("FT-10"), _row("FT-11")], with_key=False, with_header=False)
        conn, posts = _connector(monkeypatch, rendered, [page1, page2])

        invoices, partial = conn.fetch_overdue_invoices()

        assert partial is False
        numbers = [inv["invoice_number"] for inv in invoices]
        assert "FT-R" not in numbers  # righe renderizzate ignorate
        assert len(numbers) == 12
        # La prima chiamata AJAX parte da 0 con l'ordinamento imposto
        assert posts["calls"][0]["xcrud[start]"] == "0"
        assert posts["calls"][0]["xcrud[orderby]"] == "documenti.Data"
        assert posts["calls"][1]["xcrud[start]"] == "10"

    def test_boundary_duplicates_deduplicated(self, monkeypatch):
        rendered = _page([_row("FT-R")])
        page1 = _page([_row(f"FT-{i}") for i in range(10)], with_key=False, with_header=False)
        # FT-9 ricompare in pagina 2 (shuffle al confine di pagina)
        page2 = _page([_row("FT-9"), _row("FT-10")], with_key=False, with_header=False)
        conn, _ = _connector(monkeypatch, rendered, [page1, page2])

        invoices, partial = conn.fetch_overdue_invoices()

        numbers = [inv["invoice_number"] for inv in invoices]
        assert numbers.count("FT-9") == 1
        assert len(numbers) == 11
        assert partial is False

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


class TestDetailMultiAttempt:
    DETAIL_FORM = """
    <form>
      <input name="documenti.PartitaIva" value="12345678903">
      <input name="documenti.Scadenza" value="30/06/2026">
    </form>
    """
    EMPTY_SHELL = "<html><body><div>FatturaPRO</div></body></html>"

    def test_ajax_fallback_when_get_returns_shell(self, monkeypatch):
        """Il GET storico restituisce la shell senza form (il sintomo
        di produzione: 0 estratte su 588): si passa alle varianti AJAX
        xcrud e i campi arrivano dal frammento del form di edit."""
        conn = FatturaProConnector()
        conn._authenticated = True
        conn._xcrud_key = "k1"

        monkeypatch.setattr(
            conn.client, "get", lambda *a, **kw: FakeResponse(self.EMPTY_SHELL)
        )
        posts = []

        def fake_post(url, data=None, **kw):
            posts.append(dict(data))
            return FakeResponse(self.DETAIL_FORM)

        monkeypatch.setattr(conn.client, "post", fake_post)

        detail = conn.fetch_invoice_detail("111")

        assert detail["piva"] == "12345678903"
        assert str(detail["due_date"]) == "2026-06-30"
        assert posts, "le varianti AJAX devono essere tentate"
        assert posts[0]["xcrud[task]"] == "edit"
        assert posts[0]["xcrud[primary]"] == "111"

    def test_get_still_wins_when_it_has_the_form(self, monkeypatch):
        conn = FatturaProConnector()
        conn._authenticated = True
        conn._xcrud_key = "k1"
        monkeypatch.setattr(
            conn.client, "get", lambda *a, **kw: FakeResponse(self.DETAIL_FORM)
        )

        def no_post(*a, **kw):
            raise AssertionError("con il GET funzionante l'AJAX non serve")

        monkeypatch.setattr(conn.client, "post", no_post)
        detail = conn.fetch_invoice_detail("111")
        assert detail["piva"] == "12345678903"

    def test_select_scadenza_reads_selected_option(self, monkeypatch):
        html = """
        <form>
          <select name="documenti.Scadenza">
            <option value="">--</option>
            <option value="15/08/2026" selected>15/08/2026</option>
          </select>
        </form>
        """
        conn = FatturaProConnector()
        conn._authenticated = True
        monkeypatch.setattr(conn.client, "get", lambda *a, **kw: FakeResponse(html))
        monkeypatch.setattr(conn, "_get_xcrud_key", lambda: None)
        detail = conn.fetch_invoice_detail("111")
        assert str(detail["due_date"]) == "2026-08-15"

    def test_empty_filter_field_does_not_mask_real_field(self, monkeypatch):
        html = """
        <form>
          <input name="filtro.Scadenza" value="">
          <input name="documenti.Scadenza" value="20/09/2026">
        </form>
        """
        conn = FatturaProConnector()
        conn._authenticated = True
        monkeypatch.setattr(conn.client, "get", lambda *a, **kw: FakeResponse(html))
        monkeypatch.setattr(conn, "_get_xcrud_key", lambda: None)
        detail = conn.fetch_invoice_detail("111")
        assert str(detail["due_date"]) == "2026-09-20"
