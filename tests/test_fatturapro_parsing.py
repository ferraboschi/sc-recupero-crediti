"""Test parsing FatturaPro: colonne header-aware e scadenze dalla lista."""

from datetime import date

from backend.connectors.fatturapro import FatturaProConnector


LIST_HTML_WITH_SCADENZA = """
<table>
  <tr><th>Documento</th><th>Data</th><th>Scadenza</th><th>Destinatario</th><th>Totale</th><th>Saldo</th></tr>
  <tr>
    <td>FT-1</td><td>01/05/2026</td><td>30/06/2026</td><td>Rooftop SRL</td>
    <td>1.234,56</td><td>1.000,00</td>
    <td><a data-doc_id="111" href="#">apri</a></td>
  </tr>
</table>
<input type="hidden" name="key" value="abc123">
"""

LIST_HTML_CLASSIC = """
<table>
  <tr><th>Documento</th><th>Data</th><th>Destinatario</th><th>Totale</th><th>Saldo</th></tr>
  <tr><td>FT-2</td><td>02/05/2026</td><td>QOQA SA</td><td>500,00</td><td>500,00</td></tr>
</table>
"""

# Frammento AJAX (pagina 2+): NIENTE header — il mapping deve arrivare
# dalla pagina iniziale, altrimenti gli importi si disallineano.
AJAX_FRAGMENT_NO_HEADER = """
<table>
  <tr>
    <td>FT-3</td><td>03/05/2026</td><td>15/07/2026</td><td>Izakaya8 SRL</td>
    <td>750,00</td><td>250,00</td>
  </tr>
</table>
"""


class TestColumnMap:
    def test_derives_scadenza_column(self):
        conn = FatturaProConnector()
        colmap = conn._derive_column_map(LIST_HTML_WITH_SCADENZA)
        assert colmap["scadenza"] == 2
        assert colmap["saldo"] == 5

    def test_classic_layout_default(self):
        conn = FatturaProConnector()
        colmap = conn._derive_column_map(LIST_HTML_CLASSIC)
        assert colmap == {"documento": 0, "data": 1, "destinatario": 2, "totale": 3, "saldo": 4}

    def test_no_header_falls_back_to_default(self):
        conn = FatturaProConnector()
        colmap = conn._derive_column_map(AJAX_FRAGMENT_NO_HEADER)
        assert colmap["saldo"] == 4


class TestParseInvoiceTable:
    def test_parses_real_due_date_from_list(self):
        conn = FatturaProConnector()
        colmap = conn._derive_column_map(LIST_HTML_WITH_SCADENZA)
        rows = conn._parse_invoice_table(LIST_HTML_WITH_SCADENZA, colmap)
        assert len(rows) == 1
        inv = rows[0]
        assert inv["invoice_number"] == "FT-1"
        assert inv["due_date"] == date(2026, 6, 30)
        assert inv["total"] == 1234.56
        assert inv["balance"] == 1000.00

    def test_ajax_fragment_uses_page1_colmap(self):
        """Il frammento senza header parsato col mapping della pagina 1
        legge gli importi giusti anche con la colonna Scadenza in mezzo."""
        conn = FatturaProConnector()
        colmap = conn._derive_column_map(LIST_HTML_WITH_SCADENZA)
        rows = conn._parse_invoice_table(AJAX_FRAGMENT_NO_HEADER, colmap)
        assert len(rows) == 1
        assert rows[0]["invoice_number"] == "FT-3"
        assert rows[0]["balance"] == 250.00
        assert rows[0]["due_date"] == date(2026, 7, 15)

    def test_ajax_fragment_with_default_colmap_would_misparse(self):
        """Controprova del bug che la guardia previene: col layout classico
        il frammento a 6 colonne leggerebbe la scadenza come destinatario."""
        conn = FatturaProConnector()
        rows = conn._parse_invoice_table(AJAX_FRAGMENT_NO_HEADER)  # default colmap
        assert rows[0]["customer_name"] == "15/07/2026"  # disallineato!
