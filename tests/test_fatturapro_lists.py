"""Test dell'estrazione via scadenzario.php + clienti.php.

Sostituisce lo scraping del form di dettaglio (nomi campo Base64, xcrud
stateful): scadenze reali dallo scadenzario (join per numero, proroga
override, rate saldate escluse) e P.IVA/telefono/email dall'anagrafica
(join per nome). Struttura colonne verificata sui dati di produzione.
"""

from datetime import date

from backend.connectors.fatturapro import (
    FatturaProConnector, doc_key, _it_date_to_date,
)


class TestDocKey:
    def test_modern_format_with_series(self):
        # YEAR-AWARE: l'anno fa parte della chiave (numerazione riparte ogni anno).
        assert doc_key("2026/00001093/SAK - Fattura") == "2026/1093/SAK"

    def test_scadenzario_format(self):
        assert doc_key("1093/SAK del 15/06/2026") == "2026/1093/SAK"

    def test_list_and_ledger_join_to_same_key(self):
        # I due formati delle due fonti devono collassare sulla stessa chiave
        assert doc_key("2026/00000655/SAK - Fattura") == doc_key("655/SAK del 15/04/2026")

    def test_cross_year_same_number_distinct_keys(self):
        # Il cuore del bug Speranzina/Noh: 2025 e 2026 con lo STESSO numero
        # sono fatture DIVERSE → chiavi diverse, mai collidenti.
        assert doc_key("2026/00001438/SAK") != doc_key("2025/00001438/SAK")

    def test_bare_number_year_kept(self):
        assert doc_key("435/2023") == "2023/435"

    def test_zero_padded_progressivo_that_looks_like_year(self):
        # 00002023 (8 char) è il progressivo, 2026 è l'anno (ora in chiave)
        assert doc_key("2026/00002023/SAK") == "2026/2023/SAK"

    def test_no_numeric_falls_back_to_upper(self):
        assert doc_key("FT-ABC") == "FT-ABC"

    def test_unparseable_del_year_degrades_to_yearless(self):
        # Format-drift: 'del' con anno a 2 cifre → l'anno non si estrae, la
        # chiave ricade su quella SENZA anno. Degrado SICURO: un mancato match
        # dà 'assumed', mai una data di un altro anno.
        assert doc_key("1438/SAK del 17/08/26") == "1438/SAK"


# HTML di test: struttura xcrud reale (colonne verificate in produzione)

SCADENZARIO_HTML = """
<table class="xcrud-list">
<tr><th>&darr; Scadenza</th><th>Proroga</th><th>Documento</th><th>Cliente</th>
    <th>Modalità</th><th>Banca</th><th>Iban</th><th>Importo</th><th>Sospeso</th><th></th></tr>
<tr><td>20/05/2026</td><td></td><td>690/SAK del 20/04/2026</td><td>Custode srl</td>
    <td>Bonifico</td><td>Unicredit</td><td>IT..</td><td>5.080,60</td><td>5.080,60</td><td></td></tr>
<tr><td>15/07/2026</td><td></td><td>1093/SAK del 15/06/2026</td><td>Custode srl</td>
    <td>Bonifico</td><td>Unicredit</td><td>IT..</td><td>2.397,16</td><td>2.397,16</td><td></td></tr>
<tr><td>15/04/2026</td><td></td><td>655/SAK del 15/04/2026</td><td>Belfiore M &amp; M srl</td>
    <td>Bonifico</td><td>Unicredit</td><td>IT..</td><td>677,32</td><td>677,32</td><td></td></tr>
<tr><td>01/03/2026</td><td>30/06/2026</td><td>500/SAK del 01/02/2026</td><td>Con Proroga srl</td>
    <td>Bonifico</td><td>Unicredit</td><td>IT..</td><td>100,00</td><td>100,00</td><td></td></tr>
<tr><td>10/01/2026</td><td></td><td>400/SAK del 01/01/2026</td><td>Saldata srl</td>
    <td>Bonifico</td><td>Unicredit</td><td>IT..</td><td>50,00</td><td>0,00</td><td></td></tr>
<tr><td>05/02/2026</td><td></td><td>300/SAK del 01/01/2026</td><td>Rata Due srl</td>
    <td>Bonifico</td><td>Unicredit</td><td>IT..</td><td>50,00</td><td>50,00</td><td></td></tr>
<tr><td>05/01/2026</td><td></td><td>300/SAK del 01/01/2026</td><td>Rata Due srl</td>
    <td>Bonifico</td><td>Unicredit</td><td>IT..</td><td>50,00</td><td>50,00</td><td></td></tr>
</table>
"""

# Stesso numero (1438/SAK) in due anni diversi: due fatture DIVERSE, entrambe
# con rata aperta. È il caso reale del bug (La Speranzina 1438, Noh 1348).
CROSS_YEAR_HTML = """
<table class="xcrud-list">
<tr><th>Scadenza</th><th>Proroga</th><th>Documento</th><th>Cliente</th>
    <th>Modalità</th><th>Banca</th><th>Iban</th><th>Importo</th><th>Sospeso</th><th></th></tr>
<tr><td>17/09/2026</td><td></td><td>1438/SAK del 17/08/2026</td><td>La Speranzina Spa</td>
    <td>Bonifico</td><td>Unicredit</td><td>IT..</td><td>100,00</td><td>100,00</td><td></td></tr>
<tr><td>24/09/2025</td><td></td><td>1438/SAK del 24/08/2025</td><td>La Speranzina Spa</td>
    <td>Bonifico</td><td>Unicredit</td><td>IT..</td><td>200,00</td><td>200,00</td><td></td></tr>
</table>
"""

CLIENTI_HTML = """
<table class="xcrud-list">
<tr><th>Denominazione</th><th>Partita IVA</th><th>Codice Fiscale</th><th>Indirizzo</th>
    <th>Numero Civico</th><th>Cap</th><th>Comune</th><th>Provincia</th><th>Telefono</th><th>Email</th><th></th></tr>
<tr><td>Rooftop srl</td><td>18148341003</td><td>18148341003</td><td>Via X</td>
    <td>1</td><td>20100</td><td>Milano</td><td>MI</td><td>0212345</td><td>info@rooftop.it</td><td></td></tr>
<tr><td>IZAKAYA8 SRL</td><td>12911580012</td><td>12911580012</td><td>Via Y</td>
    <td>2</td><td>20100</td><td>Milano</td><td>MI</td><td></td><td>izakaya8@example.com</td><td></td></tr>
<tr><td>BATTIATO LORIS</td><td>13232070964</td><td>13232070964</td><td>Via Z</td>
    <td>3</td><td>95100</td><td>Catania</td><td>CT</td><td>095999</td><td></td><td></td></tr>
</table>
"""


def _connector_with_list(monkeypatch, path_to_html):
    """Connettore il cui _paginate_xcrud_list restituisce HTML fisso (una pagina)."""
    conn = FatturaProConnector()
    conn._authenticated = True

    def fake_paginate(path, page_size=100, max_pages=100):
        html = path_to_html.get(path, "")
        return conn._parse_xcrud_rows(html), True

    monkeypatch.setattr(conn, "_paginate_xcrud_list", fake_paginate)
    return conn


class _FakeResp:
    def __init__(self, html):
        self.text = html
        self.url = "https://cloud.fatturapro.click/scadenzario.php"

    def raise_for_status(self):
        return None


def _connector_scadenzario(monkeypatch, html):
    """Connettore per fetch_scadenze_map: client.get serve l'HTML in una
    pagina sola (niente key/instance → nessuna paginazione, complete=True)."""
    conn = FatturaProConnector()
    conn._authenticated = True
    monkeypatch.setattr(conn.client, "get", lambda *a, **kw: _FakeResp(html))
    return conn


class TestScadenzeMap:
    def test_custode_has_two_due_dates(self, monkeypatch):
        conn = _connector_scadenzario(monkeypatch, SCADENZARIO_HTML)
        smap, complete = conn.fetch_scadenze_map()
        assert complete
        assert smap[doc_key("690/SAK del 20/04/2026")] == date(2026, 5, 20)
        assert smap[doc_key("1093/SAK del 15/06/2026")] == date(2026, 7, 15)

    def test_belfiore_655_present(self, monkeypatch):
        conn = _connector_scadenzario(monkeypatch, SCADENZARIO_HTML)
        smap, _ = conn.fetch_scadenze_map()
        assert smap[doc_key("655/SAK del 15/04/2026")] == date(2026, 4, 15)

    def test_proroga_overrides_scadenza(self, monkeypatch):
        conn = _connector_scadenzario(monkeypatch, SCADENZARIO_HTML)
        smap, _ = conn.fetch_scadenze_map()
        # 500/SAK: scadenza 01/03 ma proroga 30/06 → vince la proroga
        assert smap[doc_key("500/SAK del 01/02/2026")] == date(2026, 6, 30)

    def test_settled_installment_ignored(self, monkeypatch):
        conn = _connector_scadenzario(monkeypatch, SCADENZARIO_HTML)
        smap, _ = conn.fetch_scadenze_map()
        # 400/SAK ha Sospeso=0,00 → rata saldata, non deve comparire
        assert doc_key("400/SAK del 01/01/2026") not in smap

    def test_cross_year_same_number_not_collided(self, monkeypatch):
        # REGRESSIONE Speranzina/Noh: la fattura 2026 tiene la SUA scadenza
        # (17/09/2026), NON quella dell'omonima 2025 (24/09/2025). Prima del
        # fix la nuova risultava scaduta da un anno.
        conn = _connector_scadenzario(monkeypatch, CROSS_YEAR_HTML)
        smap, _ = conn.fetch_scadenze_map()
        assert smap[doc_key("2026/00001438/SAK")] == date(2026, 9, 17)
        assert smap[doc_key("2025/00001438/SAK")] == date(2025, 9, 24)
        assert smap[doc_key("2026/00001438/SAK")] != date(2025, 9, 24)

    def test_multiple_installments_keeps_earliest(self, monkeypatch):
        conn = _connector_scadenzario(monkeypatch, SCADENZARIO_HTML)
        smap, _ = conn.fetch_scadenze_map()
        # 300/SAK ha due rate aperte (05/02 e 05/01) → tiene la più vecchia
        assert smap[doc_key("300/SAK del 01/01/2026")] == date(2026, 1, 5)

    def test_target_keys_only_covers_requested(self, monkeypatch):
        # Con target_keys si tengono solo le fatture richieste
        conn = _connector_scadenzario(monkeypatch, SCADENZARIO_HTML)
        smap, complete = conn.fetch_scadenze_map(
            target_keys={doc_key("655/SAK del 15/04/2026")}
        )
        assert complete
        assert doc_key("655/SAK del 15/04/2026") in smap
        assert doc_key("690/SAK del 20/04/2026") not in smap

    def test_convergence_stops_early_when_targets_covered(self, monkeypatch):
        # Pagina 1 (via GET) con key+instance → una rata target; le pagine
        # AJAX successive sono piene di rate saldate/altre. Coperto il target,
        # il fetch si ferma senza scorrere tutto ed è COMPLETO.
        def row(num, due, sosp):
            return (f'<tr><td>{due}</td><td></td><td>{num}</td><td>Cli</td>'
                    f'<td>B</td><td>U</td><td>I</td><td>10</td><td>{sosp}</td><td></td></tr>')
        page1 = ('<table><tr><th>Scadenza</th></tr>'
                 + row("999/SAK del 01/07/2026", "01/08/2026", "10,00")  # target, aperta
                 + '</table><input name="key" value="k1"><input name="instance" value="scad_9">')
        # pagine AJAX: 100 righe di rate SALDATE (nessun nuovo target)
        settled = ('<table>' + ''.join(
            row(f"{i}/OLD del 01/01/2023", "01/01/2023", "0,00") for i in range(100)
        ) + '</table>')

        conn = FatturaProConnector()
        conn._authenticated = True
        monkeypatch.setattr(conn.client, "get", lambda *a, **kw: _FakeResp(page1))
        posts = {"n": 0}

        def fake_post(*a, **kw):
            posts["n"] += 1
            return _FakeResp(settled)

        monkeypatch.setattr(conn.client, "post", fake_post)
        smap, complete = conn.fetch_scadenze_map(
            target_keys={doc_key("999/SAK del 01/07/2026")}, patience=3
        )
        assert complete                      # target coperto → completo
        assert smap[doc_key("999/SAK del 01/07/2026")] == date(2026, 8, 1)
        assert posts["n"] == 0               # target già in pagina 1: nessun AJAX


def _scad_row(num, due, sosp="10,00"):
    return (f'<tr><td>{due}</td><td></td><td>{num}</td><td>Cli</td>'
            f'<td>B</td><td>U</td><td>I</td><td>10</td><td>{sosp}</td><td></td></tr>')


class TestScadenzarioPaginationStart:
    """La paginazione AJAX impone il proprio ordinamento (DESC per data
    scadenza): deve quindi partire da start=0, altrimenti le prime 100 righe
    di QUELL'ordinamento — le scadenze più lontane, cioè le fatture aperte
    più recenti — non vengono mai richieste.
    """

    def test_first_ajax_page_starts_at_zero(self, monkeypatch):
        page1 = ('<table><tr><th>Scadenza</th></tr>'
                 + ''.join(_scad_row(f"{i}/SAK del 01/01/2026", "01/02/2026")
                           for i in range(10))
                 + '</table><input name="key" value="k1">'
                   '<input name="instance" value="scad_9">')
        empty = '<table><tr><th>Scadenza</th></tr></table>'

        conn = FatturaProConnector()
        conn._authenticated = True
        monkeypatch.setattr(conn.client, "get", lambda *a, **kw: _FakeResp(page1))
        starts = []

        def fake_post(*a, **kw):
            starts.append(kw["data"]["xcrud[start]"])
            return _FakeResp(empty)

        monkeypatch.setattr(conn.client, "post", fake_post)
        # target mai coperto → l'AJAX parte davvero
        conn.fetch_scadenze_map(target_keys={doc_key("777/SAK")}, patience=3)
        assert starts, "nessuna richiesta AJAX effettuata"
        assert starts[0] == "0"

    def test_due_date_in_first_100_desc_rows_is_found(self, monkeypatch):
        # Il DANNO: 250 rate ordinate DESC per scadenza. La target è la più
        # lontana nel futuro → riga 0 dell'ordinamento imposto via AJAX.
        # Partendo da start=100 non veniva mai letta: la fattura restava
        # senza scadenza reale e 'assumed' (emissione+30) la dava per
        # scaduta con 60 giorni di anticipo → sollecito indebito.
        rows = [_scad_row("9999/SAK del 01/11/2026", "01/12/2026")]
        rows += [_scad_row(f"{i}/OLD del 01/01/2024", f"{(i % 28) + 1:02d}/01/2024")
                 for i in range(249)]
        # pagina renderizzata: ordinamento di DEFAULT del sito, righe vecchie
        rendered = ('<table><tr><th>Scadenza</th></tr>' + ''.join(rows[200:210])
                    + '</table><input name="key" value="k1">'
                      '<input name="instance" value="scad_9">')

        conn = FatturaProConnector()
        conn._authenticated = True
        monkeypatch.setattr(conn.client, "get", lambda *a, **kw: _FakeResp(rendered))

        def fake_post(*a, **kw):
            start = int(kw["data"]["xcrud[start]"])
            window = rows[start:start + 100]
            return _FakeResp('<table><tr><th>Scadenza</th></tr>'
                             + ''.join(window) + '</table>')

        monkeypatch.setattr(conn.client, "post", fake_post)
        smap, complete = conn.fetch_scadenze_map(
            target_keys={doc_key("9999/SAK del 01/11/2026")}, patience=3
        )
        assert smap.get(doc_key("9999/SAK del 01/11/2026")) == date(2026, 12, 1)
        assert complete


class TestClientiMap:
    def test_piva_by_name(self, monkeypatch):
        conn = _connector_with_list(monkeypatch, {"clienti.php": CLIENTI_HTML})
        cmap, complete = conn.fetch_clienti_map()
        assert complete
        assert cmap["rooftop srl"]["piva"] == "18148341003"
        assert cmap["izakaya8 srl"]["piva"] == "12911580012"
        assert cmap["battiato loris"]["piva"] == "13232070964"

    def test_contacts_extracted(self, monkeypatch):
        conn = _connector_with_list(monkeypatch, {"clienti.php": CLIENTI_HTML})
        cmap, _ = conn.fetch_clienti_map()
        assert cmap["rooftop srl"]["phone"] == "0212345"
        assert cmap["rooftop srl"]["email"] == "info@rooftop.it"
        # IZAKAYA8: telefono vuoto ma email presente
        assert cmap["izakaya8 srl"]["phone"] is None
        assert cmap["izakaya8 srl"]["email"] == "izakaya8@example.com"

    def test_rooftop_and_qoqa_would_be_distinct(self, monkeypatch):
        # Rooftop ha P.IVA italiana valida; una QOQA svizzera avrebbe P.IVA
        # diversa → il matching per P.IVA le separa (bug 993→Rooftop).
        conn = _connector_with_list(monkeypatch, {"clienti.php": CLIENTI_HTML})
        cmap, _ = conn.fetch_clienti_map()
        assert cmap["rooftop srl"]["piva"] == "18148341003"

    def test_homonym_with_divergent_piva_is_skipped(self, monkeypatch):
        # Due entità con la STESSA denominazione ma P.IVA diverse: il nome è
        # ambiguo → non deve fornire una P.IVA (rischio abbinamento errato).
        html = """
        <table><tr><th>Denominazione</th><th>Partita IVA</th><th>Codice Fiscale</th>
        <th>Indirizzo</th><th>Civico</th><th>Cap</th><th>Comune</th><th>Prov</th>
        <th>Telefono</th><th>Email</th><th></th></tr>
        <tr><td>Bar Roma</td><td>11111111119</td><td>x</td><td>V</td><td>1</td>
        <td>1</td><td>Roma</td><td>RM</td><td>061</td><td>a@x.it</td><td></td></tr>
        <tr><td>Bar Roma</td><td>22222222220</td><td>y</td><td>V</td><td>2</td>
        <td>2</td><td>Milano</td><td>MI</td><td>022</td><td>b@x.it</td><td></td></tr>
        </table>
        """
        conn = _connector_with_list(monkeypatch, {"clienti.php": html})
        cmap, _ = conn.fetch_clienti_map()
        assert "bar roma" not in cmap

    def test_homonym_with_piva_on_one_side_only_is_skipped(self, monkeypatch):
        # Omonimi dove SOLO UNA riga ha la P.IVA: prima venivano mergiati e
        # la P.IVA di un'entità finiva servita anche per l'omonima → ambiguo.
        html = """
        <table><tr><th>Denominazione</th><th>Partita IVA</th><th>Codice Fiscale</th>
        <th>Indirizzo</th><th>Civico</th><th>Cap</th><th>Comune</th><th>Prov</th>
        <th>Telefono</th><th>Email</th><th></th></tr>
        <tr><td>Bar Roma</td><td>11111111119</td><td>x</td><td>V</td><td>1</td>
        <td>1</td><td>Roma</td><td>RM</td><td>061</td><td>a@x.it</td><td></td></tr>
        <tr><td>Bar Roma</td><td></td><td>y</td><td>V</td><td>2</td>
        <td>2</td><td>Milano</td><td>MI</td><td>022</td><td>b@x.it</td><td></td></tr>
        </table>
        """
        conn = _connector_with_list(monkeypatch, {"clienti.php": html})
        cmap, _ = conn.fetch_clienti_map()
        assert "bar roma" not in cmap

    def test_homonym_empty_row_first_still_ambiguous(self, monkeypatch):
        # Come sopra ma con la riga VUOTA prima della gemella con P.IVA:
        # l'esito non deve dipendere dall'ordine delle righe (prima la
        # riga vuota non veniva registrata e il check non scattava).
        html = """
        <table><tr><th>Denominazione</th><th>Partita IVA</th><th>Codice Fiscale</th>
        <th>Indirizzo</th><th>Civico</th><th>Cap</th><th>Comune</th><th>Prov</th>
        <th>Telefono</th><th>Email</th><th></th></tr>
        <tr><td>Bar Roma</td><td></td><td></td><td></td><td></td>
        <td></td><td></td><td></td><td></td><td></td><td></td></tr>
        <tr><td>Bar Roma</td><td>11111111119</td><td>x</td><td>V</td><td>1</td>
        <td>1</td><td>Roma</td><td>RM</td><td>061</td><td>a@x.it</td><td></td></tr>
        </table>
        """
        conn = _connector_with_list(monkeypatch, {"clienti.php": html})
        cmap, _ = conn.fetch_clienti_map()
        assert "bar roma" not in cmap

    def test_swiss_piva_with_iva_suffix_kept(self, monkeypatch):
        # Il formato svizzero ufficiale porta il suffisso ' IVA'/' MWST':
        # non fa parte del numero e non deve far scartare la P.IVA (caso
        # QOQA: senza P.IVA la contraddizione non è mai rilevabile).
        html = """
        <table><tr><th>Denominazione</th><th>Partita IVA</th><th>CF</th><th>Ind</th>
        <th>Civ</th><th>Cap</th><th>Com</th><th>Pr</th><th>Tel</th><th>Email</th><th></th></tr>
        <tr><td>QoQa Services SA</td><td>CHE-123.456.789 IVA</td><td>x</td><td>V</td><td>1</td>
        <td>1</td><td>Bulle</td><td>CH</td><td>041</td><td>q@qoqa.ch</td><td></td></tr>
        </table>
        """
        conn = _connector_with_list(monkeypatch, {"clienti.php": html})
        cmap, _ = conn.fetch_clienti_map()
        assert cmap["qoqa services sa"]["piva"] == "CHE123456789"

    def test_malformed_piva_dropped(self, monkeypatch):
        html = """
        <table><tr><th>Denominazione</th><th>Partita IVA</th><th>CF</th><th>Ind</th>
        <th>Civ</th><th>Cap</th><th>Com</th><th>Pr</th><th>Tel</th><th>Email</th><th></th></tr>
        <tr><td>Note Cliente srl</td><td>DA VERIFICARE</td><td>x</td><td>V</td><td>1</td>
        <td>1</td><td>Roma</td><td>RM</td><td>061</td><td>note@x.it</td><td></td></tr>
        </table>
        """
        conn = _connector_with_list(monkeypatch, {"clienti.php": html})
        cmap, _ = conn.fetch_clienti_map()
        # P.IVA non conforme scartata, ma i contatti restano utilizzabili
        assert cmap["note cliente srl"]["piva"] is None
        assert cmap["note cliente srl"]["email"] == "note@x.it"


class TestPaginationTokens:
    def test_reads_key_and_instance_from_hidden(self):
        conn = FatturaProConnector()
        html = ('<input type="hidden" name="key" value="abc123">'
                '<input type="hidden" name="instance" value="documenti_9f8e">')
        key, instance = conn._xcrud_tokens(html)
        assert key == "abc123"
        assert instance == "documenti_9f8e"

    def test_parse_rows_skips_header(self):
        conn = FatturaProConnector()
        rows = conn._parse_xcrud_rows(CLIENTI_HTML)
        assert len(rows) == 3
        assert rows[0][0] == "Rooftop srl"


class TestDateParsing:
    def test_it_date(self):
        assert _it_date_to_date("15/04/2026") == date(2026, 4, 15)

    def test_invalid_date(self):
        assert _it_date_to_date("") is None
        assert _it_date_to_date("nan") is None
