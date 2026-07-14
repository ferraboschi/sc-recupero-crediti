"""Test parsing FatturaPro: colonne header-aware, scadenze, guardie P.IVA."""

from datetime import date

import pytest

from backend.connectors.fatturapro import FatturaProConnector
from backend.config import config


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


def _fake_response(html: str):
    class FakeResponse:
        text = html
        url = "https://cloud.fatturapro.click/documenti.php"

        def raise_for_status(self):
            return None

    return FakeResponse()


DETAIL_HTML = """
<form>
  <input type="hidden" name="key" value="k">
  <input name="documenti.PartitaIva" value="{piva}">
  <input name="documenti.Scadenza" value="30/06/2026">
</form>
<div>P.IVA: {company}</div>
"""

DETAIL_HTML_FULLTEXT_ONLY = """
<div>Destinatario: Rooftop SRL</div>
<div>P.IVA: {piva}</div>
"""


def _valid_piva(first10="0123456789"):
    digits = [int(c) for c in first10]
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 0:
            total += d
        else:
            doubled = d * 2
            total += doubled if doubled < 10 else doubled - 9
    return first10 + str((10 - (total % 10)) % 10)


class TestInvoiceDetail:
    def _connector_with_html(self, monkeypatch, html):
        conn = FatturaProConnector()
        monkeypatch.setattr(conn.client, "get", lambda *a, **kw: _fake_response(html))
        return conn

    def test_extracts_piva_and_due_date_from_form_fields(self, monkeypatch):
        piva = _valid_piva()
        conn = self._connector_with_html(
            monkeypatch, DETAIL_HTML.format(piva=piva, company="00000000000")
        )
        detail = conn.fetch_invoice_detail("111")
        assert detail["piva"] == piva
        assert detail["piva_source"] == "field"
        assert detail["due_date"] == date(2026, 6, 30)

    def test_invalid_checksum_piva_discarded(self, monkeypatch):
        bad = _valid_piva()[:-1] + str((int(_valid_piva()[-1]) + 1) % 10)
        conn = self._connector_with_html(
            monkeypatch, DETAIL_HTML.format(piva=bad, company="x")
        )
        detail = conn.fetch_invoice_detail("111")
        assert "piva" not in detail

    def test_company_piva_blacklisted(self, monkeypatch):
        piva = _valid_piva()
        monkeypatch.setattr(config, "COMPANY_PIVA", piva)
        conn = self._connector_with_html(
            monkeypatch, DETAIL_HTML.format(piva=piva, company=piva)
        )
        detail = conn.fetch_invoice_detail("111")
        # la P.IVA del venditore non è MAI quella del destinatario
        assert "piva" not in detail

    def test_fulltext_pattern_disabled_without_company_piva(self, monkeypatch):
        """Fail-closed: senza COMPANY_PIVA il pattern full-text non gira."""
        piva = _valid_piva()
        monkeypatch.setattr(config, "COMPANY_PIVA", "")
        conn = self._connector_with_html(
            monkeypatch, DETAIL_HTML_FULLTEXT_ONLY.format(piva=piva)
        )
        detail = conn.fetch_invoice_detail("111")
        assert "piva" not in detail

    def test_fulltext_pattern_active_with_company_piva(self, monkeypatch):
        piva = _valid_piva()
        monkeypatch.setattr(config, "COMPANY_PIVA", _valid_piva("0463155037"))
        conn = self._connector_with_html(
            monkeypatch, DETAIL_HTML_FULLTEXT_ONLY.format(piva=piva)
        )
        detail = conn.fetch_invoice_detail("111")
        assert detail["piva"] == piva
        assert detail["piva_source"] == "fulltext"


class TestEnrichmentAntiRepetition:
    def test_repeated_fulltext_piva_revoked(self, monkeypatch):
        """La stessa P.IVA full-text su 3+ destinatari diversi è un valore
        fisso della pagina (es. venditore) → revocata da tutte le fatture."""
        piva = _valid_piva()
        monkeypatch.setattr(config, "COMPANY_PIVA", _valid_piva("0463155037"))
        conn = FatturaProConnector()
        html = DETAIL_HTML_FULLTEXT_ONLY.format(piva=piva)
        monkeypatch.setattr(conn.client, "get", lambda *a, **kw: _fake_response(html))

        invoices = [
            {"doc_id": str(i), "invoice_number": f"FT-{i}", "customer_name": name}
            for i, name in enumerate(["Rooftop SRL", "QOQA SA", "Izakaya8 SRL"])
        ]
        conn.enrich_invoices_from_detail(invoices, delay=0)

        assert all("customer_piva" not in inv for inv in invoices)

    def test_field_sourced_piva_not_revoked(self, monkeypatch):
        """La P.IVA da campo form esplicito è affidabile anche se ripetuta."""
        piva = _valid_piva()
        conn = FatturaProConnector()
        html = DETAIL_HTML.format(piva=piva, company="x")
        monkeypatch.setattr(conn.client, "get", lambda *a, **kw: _fake_response(html))

        invoices = [
            {"doc_id": str(i), "invoice_number": f"FT-{i}", "customer_name": name}
            for i, name in enumerate(["A SRL", "B SRL", "C SRL"])
        ]
        conn.enrich_invoices_from_detail(invoices, delay=0)

        assert all(inv.get("customer_piva") == piva for inv in invoices)
