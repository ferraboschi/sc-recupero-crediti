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
        assert doc_key("2026/00001093/SAK - Fattura") == "1093/SAK"

    def test_scadenzario_format(self):
        assert doc_key("1093/SAK del 15/06/2026") == "1093/SAK"

    def test_list_and_ledger_join_to_same_key(self):
        # I due formati delle due fonti devono collassare sulla stessa chiave
        assert doc_key("2026/00000655/SAK - Fattura") == doc_key("655/SAK del 15/04/2026")

    def test_bare_number_year_drops_year(self):
        assert doc_key("435/2023") == "435"

    def test_zero_padded_progressivo_that_looks_like_year(self):
        # 00002023 (8 char) è il progressivo, 2026 è l'anno da scartare
        assert doc_key("2026/00002023/SAK") == "2023/SAK"

    def test_no_numeric_falls_back_to_upper(self):
        assert doc_key("FT-ABC") == "FT-ABC"


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


class TestScadenzeMap:
    def test_custode_has_two_due_dates(self, monkeypatch):
        conn = _connector_with_list(monkeypatch, {"scadenzario.php": SCADENZARIO_HTML})
        smap, complete = conn.fetch_scadenze_map()
        assert complete
        assert smap[doc_key("690/SAK del 20/04/2026")] == date(2026, 5, 20)
        assert smap[doc_key("1093/SAK del 15/06/2026")] == date(2026, 7, 15)

    def test_belfiore_655_present(self, monkeypatch):
        conn = _connector_with_list(monkeypatch, {"scadenzario.php": SCADENZARIO_HTML})
        smap, _ = conn.fetch_scadenze_map()
        assert smap[doc_key("655/SAK")] == date(2026, 4, 15)

    def test_proroga_overrides_scadenza(self, monkeypatch):
        conn = _connector_with_list(monkeypatch, {"scadenzario.php": SCADENZARIO_HTML})
        smap, _ = conn.fetch_scadenze_map()
        # 500/SAK: scadenza 01/03 ma proroga 30/06 → vince la proroga
        assert smap[doc_key("500/SAK")] == date(2026, 6, 30)

    def test_settled_installment_ignored(self, monkeypatch):
        conn = _connector_with_list(monkeypatch, {"scadenzario.php": SCADENZARIO_HTML})
        smap, _ = conn.fetch_scadenze_map()
        # 400/SAK ha Sospeso=0,00 → rata saldata, non deve comparire
        assert doc_key("400/SAK") not in smap

    def test_multiple_installments_keeps_earliest(self, monkeypatch):
        conn = _connector_with_list(monkeypatch, {"scadenzario.php": SCADENZARIO_HTML})
        smap, _ = conn.fetch_scadenze_map()
        # 300/SAK ha due rate aperte (05/02 e 05/01) → tiene la più vecchia
        assert smap[doc_key("300/SAK")] == date(2026, 1, 5)


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
