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


class TestReviewRegressions:
    def test_whitespace_only_name_keeps_audit_verdict(self):
        # Nome di soli spazi = "presente" con score 0 (come l'audit vecchio):
        # con P.IVA uguale e nome vuoto/spazi resta bad, non ok.
        v = verify_invoice_customer(_Inv(PIVA_A, "   "), _Cust(PIVA_A, "Rooftop SRL"))
        assert v["verdict"] == "bad"
        assert v["level"] == "critical"

    def test_foreign_piva_match_is_not_verified(self):
        # P.IVA estera (solo formato, nessun checksum): il verde-garanzia
        # non deve scattare, anche con ragione sociale coincidente.
        v = verify_invoice_customer(_Inv("CHE123456789", "QOQA SA"),
                                    _Cust("CHE123456789", "QOQA SA"))
        assert v["verdict"] == "ok"          # audit: nessun problema
        assert v["level"] == "warning"       # semaforo: non garantibile
        assert v["guaranteed"] is False

    def test_italian_piva_match_still_verified(self):
        v = verify_invoice_customer(_Inv(PIVA_A, "ACME SRL"),
                                    _Cust(PIVA_A, "ACME S.R.L."))
        assert v["level"] == "verified"
        assert v["guaranteed"] is True


class _AcceptedName:
    def __init__(self, name_normalized):
        self.name_normalized = name_normalized


class _CustAccepted(_Cust):
    """Cliente con intestazioni accettate (come la relationship ORM: ogni
    elemento espone .name_normalized)."""

    def __init__(self, piva=None, name=None, accepted=None):
        super().__init__(piva, name)
        from backend.engine.normalizer import normalize_ragione_sociale
        self.accepted_names = [
            _AcceptedName(normalize_ragione_sociale(a)) for a in (accepted or [])
        ]


class TestAcceptedNames:
    """Conferma d'identità durevole: se l'intestazione grezza della fattura
    (normalizzata) è tra le accettate del cliente, la riga esce dai problemi
    dell'audit e il semaforo diventa verde — MA guaranteed=False (conferma
    umana, non garanzia checksum)."""

    def test_accepted_name_upgrades_critical_to_verified(self):
        # Nomi dissimili (13%), nessuna P.IVA → senza accettazione è CRITICO/bad.
        before = verify_invoice_customer(
            _Inv(None, "Sushi Kyoto"), _Cust(None, "Ferramenta Bianchi")
        )
        assert before["level"] == "critical"
        assert before["verdict"] == "bad"
        # Accettata l'intestazione "Sushi Kyoto" per questo cliente → verde.
        v = verify_invoice_customer(
            _Inv(None, "Sushi Kyoto"),
            _CustAccepted(None, "Ferramenta Bianchi", accepted=["Sushi Kyoto"]),
        )
        assert v["level"] == "verified"
        assert v["verdict"] == "ok"          # esce dai problemi dell'audit
        assert v["guaranteed"] is False      # conferma umana, non checksum
        assert v["manual_confirmed"] is True
        assert "Confermato a mano" in v["message"]

    def test_accepted_name_upgrades_warning_to_verified(self):
        # Caso senza P.IVA da assegnare: nomi coincidenti ma nessuna P.IVA →
        # senza accettazione resta warning (non garantibile).
        v = verify_invoice_customer(
            _Inv(None, "Belfiore M & M srl"),
            _CustAccepted(None, "Belfiore M&M srl", accepted=["Belfiore M & M srl"]),
        )
        assert v["level"] == "verified"
        assert v["guaranteed"] is False
        assert v["manual_confirmed"] is True

    def test_piva_conflict_valve_blocks_upgrade(self):
        # VALVOLA OBBLIGATORIA: una conferma d'intestazione NON deve MAI
        # zittire due P.IVA valide e DIVERSE — resta critical.
        v = verify_invoice_customer(
            _Inv(PIVA_A, "Sushi Kyoto"),
            _CustAccepted(PIVA_B, "Ferramenta Bianchi", accepted=["Sushi Kyoto"]),
        )
        assert v["level"] == "critical"
        assert v["verdict"] == "bad"
        assert v["piva_conflict"] is True
        assert v["manual_confirmed"] is False
        assert "DIVERSA" in v["message"]

    def test_accepted_name_no_match_no_upgrade(self):
        # L'intestazione accettata è un'ALTRA: nessun upgrade.
        v = verify_invoice_customer(
            _Inv(None, "Sushi Kyoto"),
            _CustAccepted(None, "Ferramenta Bianchi", accepted=["Gamma Delta"]),
        )
        assert v["level"] == "critical"
        assert v["manual_confirmed"] is False

    def test_checksum_verified_stays_guaranteed(self):
        # Un verde da checksum reale resta guaranteed=True anche con la lista
        # accettate presente (non viene "declassato" a conferma umana).
        v = verify_invoice_customer(
            _Inv(PIVA_A, "ACME SRL"),
            _CustAccepted(PIVA_A, "ACME S.R.L.", accepted=["ACME SRL"]),
        )
        assert v["level"] == "verified"
        assert v["guaranteed"] is True
        assert v["manual_confirmed"] is False

    def test_customer_without_relationship_does_not_explode(self):
        # I chiamanti che passano un customer "simulato" (SimpleNamespace,
        # niente accepted_names) non devono far esplodere verify.
        v = verify_invoice_customer(_Inv(None, "Alfa Beta"), _Cust(None, "Zeta Omega"))
        assert v["manual_confirmed"] is False


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
