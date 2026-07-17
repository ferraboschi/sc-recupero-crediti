"""Test del controllo puntuale fattura ↔ cliente (backend/engine/verify.py).

Il verde ("verified") è una GARANZIA: compare SOLO con P.IVA presente su
entrambi, checksum-valida, uguale, e ragione sociale coincidente.
"""

from backend.engine.verify import verify_invoice_customer

# P.IVA italiane checksum-valide
PIVA_A = "12345678903"
PIVA_B = "98765432103"


class _Inv:
    def __init__(self, piva=None, name=None):
        self.customer_piva_raw = piva
        self.customer_name_raw = name


class _Cust:
    def __init__(self, piva=None, name=None):
        self.partita_iva = piva
        self.ragione_sociale = name


class TestVerified:
    def test_full_match_is_verified(self):
        v = verify_invoice_customer(_Inv(PIVA_A, "QoQa Services SA"),
                                    _Cust(PIVA_A, "QoQa Services SA"))
        assert v["level"] == "verified"
        assert v["verdict"] == "ok"
        assert v["guaranteed"] is True
        assert "garantisco" in v["message"]

    def test_piva_match_legal_form_variation_is_verified(self):
        # "ACME SRL" vs "ACME S.R.L." → stessa entità, P.IVA uguale.
        v = verify_invoice_customer(_Inv(PIVA_A, "ACME SRL"),
                                    _Cust(PIVA_A, "ACME S.R.L."))
        assert v["level"] == "verified"

    def test_person_name_with_matching_piva_is_verified(self):
        # Ditta individuale: fattura intestata alla persona, P.IVA uguale.
        v = verify_invoice_customer(
            _Inv(PIVA_A, "MERCURI CHRISTIAN"),
            _Cust(PIVA_A, "Dr. Gahe di Mercuri Christian"))
        assert v["level"] == "verified"


class TestCritical:
    def test_piva_conflict_is_critical(self):
        v = verify_invoice_customer(_Inv(PIVA_A, "QOQA SRL"),
                                    _Cust(PIVA_B, "Rooftop SRL"))
        assert v["level"] == "critical"
        assert v["verdict"] == "bad"
        assert v["piva_conflict"] is True

    def test_poisoned_piva_is_critical(self):
        # P.IVA uguale ma nomi del tutto diversi: sospetta.
        v = verify_invoice_customer(_Inv(PIVA_A, "Foo SRL"),
                                    _Cust(PIVA_A, "Bar SPA"))
        assert v["level"] == "critical"
        assert v["verdict"] == "bad"

    def test_dissimilar_names_no_piva_is_critical(self):
        v = verify_invoice_customer(_Inv(None, "Alfa SRL"),
                                    _Cust(None, "Zeta SPA"))
        assert v["level"] == "critical"


class TestWarning:
    def test_piva_only_on_invoice_is_warning(self):
        v = verify_invoice_customer(_Inv(PIVA_A, "QOQA SRL"),
                                    _Cust(None, "QOQA SRL"))
        assert v["level"] == "warning"
        assert v["guaranteed"] is False

    def test_no_piva_both_sides_names_equal_is_warning_not_verified(self):
        # Nomi coincidenti ma nessuna P.IVA: non garantibile → warning.
        v = verify_invoice_customer(_Inv(None, "Belfiore M & M srl"),
                                    _Cust(None, "Belfiore M&M srl"))
        assert v["level"] == "warning"
        assert v["guaranteed"] is False

    def test_no_customer_is_warning(self):
        v = verify_invoice_customer(_Inv(PIVA_A, "QOQA"), None)
        assert v["level"] == "warning"

    def test_side_by_side_values_present(self):
        v = verify_invoice_customer(_Inv(PIVA_A, "QOQA SRL"),
                                    _Cust(PIVA_B, "Rooftop SRL"))
        assert v["invoice_piva"] == PIVA_A
        assert v["customer_piva"] == PIVA_B
        assert v["invoice_name"] == "QOQA SRL"
        assert v["customer_name"] == "Rooftop SRL"


class TestAuditVerdictUnchanged:
    """Il verdict (audit) resta più permissivo del level (semaforo):
    P.IVA uguale con nome decente = ok, ma non necessariamente verified."""

    def test_piva_match_weak_name_is_ok_but_not_verified(self):
        # nome simile ma non coincidente (score 40-99): audit ok, semaforo warning.
        v = verify_invoice_customer(_Inv(PIVA_A, "Trattoria Blu"),
                                    _Cust(PIVA_A, "Trattoria Blu Mare Aperto"))
        # stessa entità (P.IVA), ma il nome non è pienamente coincidente
        if v["name_equivalent"]:
            assert v["level"] == "verified"
        else:
            assert v["verdict"] == "ok"
            assert v["level"] == "warning"
